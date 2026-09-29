"""decision-svc FastAPI app (spec §11.12)."""

from __future__ import annotations

import asyncio
import contextlib
import random
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fsp_shared import db
from fsp_shared.breaker import CircuitBreaker
from fsp_shared.bus import Bus
from fsp_shared.config import Settings, get_settings
from fsp_shared.logging import configure_logging, get_logger
from fsp_shared.metrics import install_metrics
from fsp_shared.schemas import BusEventType
from fsp_shared.sim_client import SimClient
from pydantic import BaseModel, Field
from sqlalchemy import text

from .forecasts import ForecastClient
from .loop import DecisionEngine
from .repo import Repo
from .triage.jev import JevTriage
from .whatif import WhatIfProposal, WhatIfRequest, run_whatif
from .world import load_world

SERVICE = "decision-svc"
log = get_logger("decision.main")
RECONCILE_INTERVAL_S = 5.0
POLL_INTERVAL_S = 1.0
IMMEDIATE_EVENTS = {"route_disruption", "demand_spike", "station_outage", "depot_constraint", "shipment_delay",
                    "supply_shortfall"}


class ApproveBody(BaseModel):
    operator: str = "operator"
    note: str | None = None
    qty: float | None = Field(default=None, gt=0)
    route_id: str | None = None


class RejectBody(BaseModel):
    operator: str = "operator"
    note: str | None = None


class ManualBody(BaseModel):
    station_id: str
    fuel_type: str
    qty: float = Field(gt=0)
    route_id: str
    operator: str = "operator"
    note: str | None = None
    staged: bool = False  # true = create a STAGED_REVIEW decision only (copilot "propose")
    origin: str = "OPERATOR_MANUAL"


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.engine = db.make_engine(settings.DATABASE_URL)
        self.repo = Repo(self.engine)
        self.client = SimClient.from_settings(settings)
        self.forecast_breaker = CircuitBreaker("forecast", settings.BREAKER_FAILURE_THRESHOLD,
                                               settings.BREAKER_OPEN_SECONDS, timeout=0.5)
        self.jev_breaker = CircuitBreaker("jev", settings.BREAKER_FAILURE_THRESHOLD, settings.BREAKER_OPEN_SECONDS,
                                          timeout=settings.JEV_TIMEOUT_MS / 1000)
        self.forecast = ForecastClient(settings.FORECAST_URL, self.forecast_breaker)
        self.jev = JevTriage(settings.TYPESAFE_API_KEY, self.jev_breaker, settings.JEV_TIMEOUT_MS,
                             bad_key=settings.CHAOS_BAD_AI_KEYS, log_call=self.repo.ai_call,
                             model=None if settings.JEV_MODEL in ("", "jev-latest") else settings.JEV_MODEL)
        self.bus: Bus | None = None
        self.decision = DecisionEngine(settings, self.engine, self.client, self.repo, self.forecast, self.jev,
                                       self.jev_breaker)
        self.tasks: list[asyncio.Task] = []
        self._ticks_since_cycle = 0
        self._last_tick_seen: int | None = None
        self.redis_ok = False

    async def start(self) -> None:
        await db.wait_for_schema(self.engine, timeout_s=180)
        with contextlib.suppress(Exception):  # resume anything left COMMITTING by a crash (same keys)
            await self.decision.executor.reconcile(await load_world(self.engine))
        self.tasks = [
            asyncio.create_task(self._bus_loop(), name="bus-trigger"),
            asyncio.create_task(self._poll_loop(), name="poll-trigger"),
            asyncio.create_task(self._reconcile_loop(), name="reconciler"),
        ]

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.decision.aclose()
        await self.forecast.aclose()
        await self.client.aclose()
        if self.bus:
            with contextlib.suppress(Exception):
                await self.bus.close()
        await self.engine.dispose()

    # --- triggers ------------------------------------------------------------------------------------------------------
    def _fire(self, reason: str) -> None:
        asyncio.get_running_loop().create_task(self.decision.run_cycle(reason=reason))

    def _on_tick(self, tick: int) -> None:
        if self._last_tick_seen is not None and tick < self._last_tick_seen:
            self._ticks_since_cycle = self.s.DECISION_INTERVAL_TICKS  # reset: plan immediately
        elif self._last_tick_seen is not None:
            self._ticks_since_cycle += tick - self._last_tick_seen
        else:
            self._ticks_since_cycle = self.s.DECISION_INTERVAL_TICKS
        self._last_tick_seen = tick
        if self._ticks_since_cycle >= self.s.DECISION_INTERVAL_TICKS:
            self._ticks_since_cycle = 0
            self._fire("tick")

    async def _bus_loop(self) -> None:
        backoff = 0.5
        while True:
            try:
                self.bus = self.bus or Bus.from_url(self.s.REDIS_URL)
                self.decision.bus = self.bus
                await self.bus.ensure_group("decision-svc")
                self.redis_ok = True
                async for msg in self.bus.listen("decision-svc", "decision-1"):
                    backoff = 0.5
                    await self._on_bus(msg.type, msg.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Redis down: the poll loop takes over
                self.redis_ok = False
                self.bus = None
                self.decision.bus = None
                log.warning("bus_unavailable", error=str(exc)[:150])
                await asyncio.sleep(random.uniform(backoff / 2, backoff))
                backoff = min(30.0, backoff * 2)

    async def _on_bus(self, typ: str, p: dict[str, Any]) -> None:
        if typ == BusEventType.STATE_UPDATED and isinstance(p.get("tick"), int):
            self._on_tick(p["tick"])
        elif typ == BusEventType.EVENT_CHANGED and p.get("type") in IMMEDIATE_EVENTS:
            self._fire(f"event:{p.get('type')}:{p.get('status')}")
            if p.get("status") == "ACTIVE":
                await self.decision.request_incident(p.get("id"), f"{p.get('type')} became ACTIVE")
        elif typ == BusEventType.ALLOCATION_CHANGED and p.get("status") == "FAILED":
            self._fire("allocation_failed")
        elif typ == BusEventType.SIM_FAULT and p.get("type") == "stale_data":
            self.decision.sim_fault_stale = bool(p.get("active"))
        elif typ == BusEventType.SIM_RESET:
            self.decision.reset()
            self._last_tick_seen = None

    async def _poll_loop(self) -> None:
        """Fallback when Redis is down: watch sim_ticks directly (spec §14.1)."""
        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            if self.redis_ok:
                continue
            try:
                async with self.engine.connect() as c:
                    tick = (await c.execute(text("SELECT max(tick) FROM sim_ticks"))).scalar_one()
                if tick is not None:
                    self._on_tick(int(tick))
            except Exception:  # noqa: BLE001
                pass

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(RECONCILE_INTERVAL_S)
            try:
                if await self.repo.by_status("COMMITTING"):
                    await self.decision.executor.reconcile(await load_world(self.engine))
                for d in await self.repo.by_status("AUTO_APPROVED", "OPERATOR_APPROVED"):  # DISPATCH_CAPACITY retries
                    await self.decision.executor.execute(d, await load_world(self.engine))
            except Exception as exc:  # noqa: BLE001
                log.warning("reconcile_failed", error=str(exc)[:150])


def create_app(runtime_factory=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        configure_logging(SERVICE, settings.LOG_LEVEL)
        rt = runtime_factory(settings) if runtime_factory else Runtime(settings)
        app.state.runtime = rt
        await rt.start()
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title="decision-svc", version="0.1.0", lifespan=lifespan)
    install_metrics(app, SERVICE)

    def rt() -> Runtime:
        return app.state.runtime

    @app.get("/health")
    async def health() -> dict[str, Any]:
        r: Runtime | None = getattr(app.state, "runtime", None)
        if r is None:
            return {"status": "starting"}
        ok = await db.ping(r.engine)
        return {"status": "ok" if ok else "degraded", "database": "ok" if ok else "down", "mode": r.decision.mode()}

    @app.get("/status")
    async def status() -> dict[str, Any]:
        r = rt()
        last = r.decision.last
        return {
            "mode": r.decision.mode(),
            "breakers": {"forecast": r.forecast_breaker.state.name, "jev": r.jev_breaker.state.name},
            "jev_enabled": r.jev.enabled,
            "redis": r.redis_ok,
            "stale": r.decision.sim_fault_stale or bool(r.decision.last_world and r.decision.last_world.stale),
            "last_cycle": None if last is None else {
                "tick": last.tick, "planner": last.planner, "forecast_source": last.forecast_source,
                "proposals": len(last.proposals), "dropped": len(last.dropped), "alerts": len(last.alerts),
                "cancelled": last.cancelled, "duration_ms": last.duration_ms, "skipped": last.skipped,
            },
        }

    @app.post("/cycle/run")
    async def cycle_run(dry_run: bool = False) -> dict[str, Any]:
        res = await rt().decision.run_cycle(dry_run=dry_run, reason="api")
        return res.__dict__

    @app.post("/whatif")
    async def whatif(req: WhatIfRequest | WhatIfProposal) -> dict[str, Any]:
        r = rt()
        if isinstance(req, WhatIfProposal):
            req = WhatIfRequest(proposals=[req])
        world = await load_world(r.engine)
        if world is None:
            raise HTTPException(503, "no simulator state yet")
        forecasts, source = await r.forecast.get(world, max(req.horizon, r.s.HORIZON_TICKS))
        out = run_whatif(world, forecasts, req, derate=r.s.DEPOT_CONSTRAINT_DERATE)
        out["forecast_source"] = source
        return out

    @app.get("/decisions")
    async def decisions(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return await rt().repo.list_decisions(status, min(500, limit))

    @app.get("/decisions/{decision_id}")
    async def decision(decision_id: str) -> dict[str, Any]:
        d = await rt().repo.get_decision(decision_id)
        if d is None:
            raise HTTPException(404, "decision not found")
        return d

    @app.post("/decisions/{decision_id}/approve")
    async def approve(decision_id: str, body: ApproveBody) -> dict[str, Any]:
        try:
            return await rt().decision.approve(decision_id, operator=body.operator, note=body.note, qty=body.qty,
                                               route_id=body.route_id)
        except KeyError as exc:
            raise HTTPException(404, "decision not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/decisions/{decision_id}/reject")
    async def reject(decision_id: str, body: RejectBody) -> dict[str, Any]:
        try:
            return await rt().decision.reject(decision_id, operator=body.operator, note=body.note)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/decisions/manual")
    async def manual(body: ManualBody) -> dict[str, Any]:
        if body.origin not in ("OPERATOR_MANUAL", "SYSTEM2_OVERRIDE"):
            raise HTTPException(422, "origin must be OPERATOR_MANUAL or SYSTEM2_OVERRIDE")
        try:
            return await rt().decision.manual(station_id=body.station_id, fuel=body.fuel_type, qty=body.qty,
                                              route_id=body.route_id, operator=body.operator, note=body.note,
                                              staged=body.staged, origin=body.origin)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return app


app = create_app()
