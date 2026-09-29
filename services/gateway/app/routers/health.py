"""Aggregated health console (spec §15.3): one row per component with Healthy / Degraded / Down, plus live p95
latency and error rate from Prometheus. Every probe runs concurrently with a short timeout, so a dead dependency costs
at most ~1 s and never takes the console down with it."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request
from fsp_shared import db
from fsp_shared.exceptions import SimClientError

from ..runtime import Runtime, Upstream

router = APIRouter(prefix="/api", tags=["health"])

HEALTHY, DEGRADED, DOWN = "Healthy", "Degraded", "Down"
RANK = {HEALTHY: 0, DEGRADED: 1, DOWN: 2}
PROBE_TIMEOUT_S = 1.0

PROM_QUERIES = {
    "p95_latency_ms": 'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket'
                      '{route!~"/metrics|/health"}[2m]))) * 1000',
    "error_rate_pct": '100 * sum(rate(http_requests_total{status=~"5.."}[2m])) / '
                      'clamp_min(sum(rate(http_requests_total[2m])), 1e-9)',
    "rps": 'sum(rate(http_requests_total{route!~"/metrics|/health"}[1m]))',
    "fallback_activations": "sum(fallback_activations_total)",
    "sim_client_p95_ms": "histogram_quantile(0.95, sum by (le) (rate(sim_client_latency_seconds_bucket[2m]))) * 1000",
}


def comp(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, **extra}


async def _prom(rt: Runtime) -> dict[str, Any]:
    async def q(expr: str) -> float | None:
        up = await rt.call("GET", f"{rt.s.PROMETHEUS_URL}/api/v1/query", params={"query": expr}, timeout=PROBE_TIMEOUT_S)
        if not up.ok:
            return None
        try:
            res = up.data["data"]["result"]
            v = float(res[0]["value"][1]) if res else None
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        return None if v is None or v != v else round(v, 2)  # NaN -> None

    vals = await asyncio.gather(*(q(e) for e in PROM_QUERIES.values()))
    out = dict(zip(PROM_QUERIES, vals, strict=True))
    out["available"] = any(v is not None for v in vals)
    return out


async def components(rt: Runtime) -> dict[str, Any]:
    started = time.perf_counter()

    async def database() -> dict[str, Any]:
        t = time.perf_counter()
        ok = await db.ping(rt.engine)
        return comp("Database", HEALTHY if ok else DOWN, "PostgreSQL reachable" if ok else "PostgreSQL unreachable",
                    latency_ms=round((time.perf_counter() - t) * 1000, 1))

    async def redis() -> dict[str, Any]:
        t = time.perf_counter()
        try:
            await asyncio.wait_for(rt.redis.ping(), PROBE_TIMEOUT_S)
            return comp("Redis", HEALTHY, "event bus reachable", latency_ms=round((time.perf_counter() - t) * 1000, 1))
        except Exception as exc:  # noqa: BLE001
            return comp("Redis", DOWN, f"unreachable ({type(exc).__name__}); services fall back to polling")

    async def simulator() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        t = time.perf_counter()
        try:
            h = await asyncio.wait_for(rt.sim.health(), PROBE_TIMEOUT_S * 2)
            health_ok = h.data.status == "ok"
        except (SimClientError, TimeoutError):
            health_ok = False
        lat = round((time.perf_counter() - t) * 1000, 1)
        ing = await rt.ingestion_status()
        if not ing.ok:
            ingestion = comp("Ingestion", DOWN, f"ingestion-svc unreachable ({ing.error})")
            sse = comp("SSE stream", DOWN, "unknown: ingestion-svc is down")
            sim_state, stale, age = ("UP" if health_ok else "DOWN"), False, None
        else:
            d = ing.data
            lag, age = d.get("sync_lag_ticks") or 0, d.get("last_sync_age_s")
            ing_status = HEALTHY if d.get("last_sync_ok") and lag <= 8 else DEGRADED
            ingestion = comp("Ingestion", ing_status,
                             f"last sync tick {d.get('last_synced_tick')}, lag {lag} ticks, "
                             f"{d.get('last_sync_latency_ms')} ms", sync_lag_ticks=lag,
                             latency_ms=d.get("last_sync_latency_ms"))
            s = d.get("sse") or {}
            if s.get("connected"):
                sse = comp("SSE stream", HEALTHY, f"connected, {s.get('reconnects', 0)} reconnects")
            else:
                sse = comp("SSE stream", DEGRADED,
                           f"disconnected for {s.get('down_for_s')} s"
                           f"{' (stream_disconnect fault)' if s.get('stream_fault') else ''}; REST polling fallback")
            sim_state, stale = (d.get("simulator") or {}).get("state", "UNKNOWN"), bool(d.get("stale"))
        if not health_ok:
            simc = comp("Simulator connection", DOWN, "/v1/health unreachable", latency_ms=lat)
        elif stale:
            simc = comp("Simulator connection", DEGRADED, "STALE DATA (X-Simulator-Stale): auto-dispatch suspended",
                        latency_ms=lat, stale=True)
        elif sim_state == "DEGRADED":
            simc = comp("Simulator connection", DEGRADED, "/v1/* failing while /v1/health answers: fault active",
                        latency_ms=lat)
        else:
            simc = comp("Simulator connection", HEALTHY, f"UP, last successful sync {age} s ago", latency_ms=lat)
        return simc, sse, ingestion

    async def forecast() -> dict[str, Any]:
        up = await rt.call("GET", f"{rt.s.FORECAST_URL}/health", timeout=PROBE_TIMEOUT_S)
        if not up.ok:
            return comp("Forecast (inference)", DOWN, f"forecast-svc unreachable ({up.error}); baseline fallback")
        mode = up.data.get("mode")
        model = (up.data.get("model") or {}).get("version")
        if up.data.get("status") != "ok":
            return comp("Forecast (inference)", DEGRADED, f"database {up.data.get('database')}", latency_ms=up.latency_ms)
        return comp("Forecast (inference)", HEALTHY if mode == "MODEL" else DEGRADED,
                    f"model {model}" if model else "cold start: baseline formula", latency_ms=round(up.latency_ms, 1))

    async def decision() -> tuple[dict[str, Any], dict[str, Any]]:
        up = await rt.decision_status()
        if not up.ok:
            return (comp("Decision engine", DOWN, f"decision-svc unreachable ({up.error})"),
                    comp("Jev (System 1)", DOWN, "unknown: decision-svc is down"))
        d = up.data
        mode = d.get("mode")
        last = d.get("last_cycle") or {}
        detail = (f"mode {mode}, planner {last.get('planner', '-')}, forecast {last.get('forecast_source', '-')}, "
                  f"last cycle {last.get('duration_ms', '-')} ms @ tick {last.get('tick', '-')}")
        dec = comp("Decision engine", HEALTHY if mode == "NORMAL" else DEGRADED, detail, mode=mode,
                   breakers=d.get("breakers"))
        jb = (d.get("breakers") or {}).get("jev")
        if not d.get("jev_enabled"):
            jev = comp("Jev (System 1)", DEGRADED, "no TYPESAFE_API_KEY: deterministic urgency + heuristic gate",
                       breaker=jb)
        else:
            jev = comp("Jev (System 1)", {"CLOSED": HEALTHY, "HALF_OPEN": DEGRADED}.get(jb, DOWN),
                       f"breaker {jb}" + ("" if jb == "CLOSED" else ": deterministic fallback active"), breaker=jb)
        return dec, jev

    async def gemini() -> dict[str, Any]:
        up = await rt.call("GET", f"{rt.s.COGNITIVE_URL}/health", timeout=PROBE_TIMEOUT_S)
        if not up.ok:
            return comp("Gemini (System 2)", DOWN, f"cognitive-svc unreachable ({up.error}); template explanations")
        g = up.data.get("gemini") or {}
        if not g.get("enabled"):
            return comp("Gemini (System 2)", DEGRADED, "no usable GEMINI_API_KEY/model: template explanations",
                        breaker=g.get("breaker"))
        b = g.get("breaker")
        return comp("Gemini (System 2)", {"CLOSED": HEALTHY, "HALF_OPEN": DEGRADED}.get(b, DOWN),
                    f"{g.get('model')}, breaker {b}", breaker=b)

    gathered = await asyncio.gather(database(), redis(), simulator(), forecast(), decision(), gemini(), _prom(rt),
                                    return_exceptions=True)
    names = ["Database", "Redis", "Simulator", "Forecast", "Decision", "Gemini", "prom"]
    items: list[dict[str, Any]] = [comp("Backend API (gateway)", HEALTHY, "serving requests")]
    prom: dict[str, Any] = {"available": False}
    for n, g in zip(names, gathered, strict=True):
        if isinstance(g, BaseException):
            items.append(comp(n, DOWN, f"probe error: {type(g).__name__}"))
        elif n == "prom":
            prom = g
        elif isinstance(g, tuple):
            items.extend(g)
        else:
            items.append(g)
    order = ["Backend API (gateway)", "Database", "Redis", "Simulator connection", "SSE stream", "Ingestion",
             "Forecast (inference)", "Decision engine", "Jev (System 1)", "Gemini (System 2)"]
    items.sort(key=lambda c: order.index(c["name"]) if c["name"] in order else 99)
    overall = max((c["status"] for c in items), key=lambda s: RANK.get(s, 2))
    return {"overall": overall, "components": items, "metrics": prom, "grafana_url": rt.s.GRAFANA_PUBLIC_URL,
            "probe_ms": round((time.perf_counter() - started) * 1000, 1), "at": time.time()}


@router.get("/health/components")
async def health_components(request: Request) -> dict[str, Any]:
    rt: Runtime = request.app.state.runtime
    return (await rt.cached("health_components", 1.5, lambda: _wrap(rt))).data


async def _wrap(rt: Runtime) -> Upstream:
    return Upstream(await components(rt), None, 0.0)
