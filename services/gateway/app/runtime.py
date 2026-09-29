"""What gateway-svc owns at runtime: DB engine, HTTP client for the internal services, simulator client (health +
optional demo controls), Redis and a tiny TTL cache so N browser tabs do not multiply upstream load."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import redis.asyncio as aioredis
from fsp_shared import db
from fsp_shared.config import Settings
from fsp_shared.logging import get_logger
from fsp_shared.sim_client import SimClient
from sqlalchemy import text

from .ws import Hub

log = get_logger("gateway.runtime")


class Upstream:
    """Result of an internal-service call: ``data`` is None when the service is down/slow (``error`` says why)."""

    __slots__ = ("data", "error", "latency_ms", "status")

    def __init__(self, data: Any, error: str | None, latency_ms: float, status: int = 0) -> None:
        self.data = data
        self.error = error
        self.latency_ms = latency_ms
        self.status = status

    @property
    def ok(self) -> bool:
        return self.error is None


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.engine = db.make_engine(settings.DATABASE_URL, pool_size=10)
        self.http = httpx.AsyncClient(timeout=5.0, limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))
        self.sim = SimClient(settings.SIMULATOR_URL, settings.SIM_HTTP_TIMEOUT_S)
        self.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=2.0,
                                       socket_connect_timeout=1.0)
        self.hub = Hub(self)
        self._cache: dict[str, tuple[float, Upstream]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await db.wait_for_schema(self.engine, timeout_s=180)
        self.tasks = [asyncio.create_task(self.hub.run_bus(), name="ws-bus"),
                      asyncio.create_task(self.hub.run_health(), name="ws-health")]

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.hub.close_all()
        await self.http.aclose()
        await self.sim.aclose()
        try:
            await self.redis.aclose()
        except Exception:  # noqa: BLE001
            pass
        await self.engine.dispose()

    # --- database ------------------------------------------------------------------------------------------------
    async def rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        async with self.engine.connect() as c:
            return [dict(r) for r in (await c.execute(text(sql), params)).mappings().all()]

    async def one(self, sql: str, **params: Any) -> dict[str, Any] | None:
        rows = await self.rows(sql, **params)
        return rows[0] if rows else None

    async def audit(self, *, actor: str, action: str, entity_type: str, entity_id: str | None, result: str,
                    data: dict | None = None) -> None:
        import json

        try:
            async with self.engine.begin() as c:
                await c.execute(
                    text("INSERT INTO audit_log (tick, actor, action, entity_type, entity_id, result, data) VALUES "
                         "((SELECT max(tick) FROM sim_ticks), :a, :ac, :et, :e, :r, CAST(:d AS jsonb))"),
                    {"a": actor, "ac": action, "et": entity_type, "e": entity_id, "r": result,
                     "d": json.dumps(data or {}, default=str)},
                )
        except Exception as exc:  # noqa: BLE001 - auditing must never break the request
            log.error("audit_failed", action=action, error=str(exc)[:200])

    # --- internal services --------------------------------------------------------------------------------------
    async def call(self, method: str, url: str, *, timeout: float = 2.0, **kw: Any) -> Upstream:
        started = time.perf_counter()
        try:
            r = await self.http.request(method, url, timeout=timeout, **kw)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return Upstream(None, f"{type(exc).__name__}", (time.perf_counter() - started) * 1000)
        ms = (time.perf_counter() - started) * 1000
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:300]}
        if r.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else body
            return Upstream(body, f"HTTP {r.status_code}: {str(detail)[:200]}", ms, r.status_code)
        return Upstream(body, None, ms, r.status_code)

    async def cached(self, key: str, ttl: float, fn) -> Upstream:
        """Single-flight TTL cache for read-mostly upstream calls (forecasts, decision status, health)."""
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < ttl:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            val = await fn()
            self._cache[key] = (time.monotonic(), val)
            return val

    async def forecasts(self) -> Upstream:
        return await self.cached("forecasts", 1.0, lambda: self.call("GET", f"{self.s.FORECAST_URL}/forecast",
                                                                      timeout=1.0))

    async def decision_status(self) -> Upstream:
        return await self.cached("decision_status", 1.0,
                                 lambda: self.call("GET", f"{self.s.DECISION_URL}/status", timeout=1.0))

    async def ingestion_status(self) -> Upstream:
        return await self.cached("ingestion_status", 1.0,
                                 lambda: self.call("GET", f"{self.s.INGESTION_URL}/status", timeout=1.0))
