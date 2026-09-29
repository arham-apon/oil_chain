"""Read-only operational state for the UI (spec §13.1). Everything comes from Postgres (written by ingestion-svc) plus
forecast-svc / decision-svc status; nothing here calls the simulator's ``/v1`` API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fsp_shared import world_constants as wc
from sqlalchemy import text

from ..display import name, station_meta
from ..runtime import Runtime

router = APIRouter(prefix="/api", tags=["state"])
RESERVING = ["STAGED_REVIEW", "AUTO_APPROVED", "OPERATOR_APPROVED", "COMMITTING"]
DAY_TICKS = 96


def _rt(request: Request) -> Runtime:
    return request.app.state.runtime


async def _latest_tick(rt: Runtime) -> dict[str, Any] | None:
    return await rt.one("SELECT tick, sim_time, status, tick_minutes, stale, recorded_at FROM sim_ticks "
                        "ORDER BY tick DESC LIMIT 1")


async def _forecast_index(rt: Runtime) -> tuple[dict[tuple[str, str], dict], str | None]:
    up = await rt.forecasts()
    if not up.ok or not isinstance(up.data, dict):
        return {}, up.error or "forecast unavailable"
    return {(f["station_id"], f["fuel_type"]): f for f in up.data.get("forecasts", [])}, None


# --------------------------------------------------------------------------------------------------------------------
@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    rt = _rt(request)
    tick = await _latest_tick(rt)
    metrics = await rt.one("SELECT * FROM sim_metrics ORDER BY tick DESC LIMIT 1")
    prev = None
    if metrics:
        prev = await rt.one("SELECT * FROM sim_metrics WHERE tick <= :t ORDER BY tick DESC LIMIT 1",
                            t=metrics["tick"] - DAY_TICKS)
    counts = await rt.one(
        "SELECT (SELECT count(*) FROM sim_allocations WHERE status IN ('PENDING','IN_TRANSIT')) AS in_flight, "
        "(SELECT coalesce(sum(quantity),0) FROM sim_allocations WHERE status IN ('PENDING','IN_TRANSIT')) "
        "  AS in_flight_liters, "
        "(SELECT count(*) FROM decisions WHERE status = 'STAGED_REVIEW') AS staged, "
        "(SELECT count(*) FROM decisions WHERE status = 'COMMITTED') AS committed, "
        "(SELECT count(*) FROM alerts WHERE NOT coalesce(acknowledged,false) AND tick >= :since) AS alerts_open, "
        "(SELECT count(*) FROM alerts WHERE NOT coalesce(acknowledged,false) AND tick >= :since "
        "   AND severity IN ('HIGH','CRITICAL')) AS alerts_high, "
        "(SELECT count(*) FROM sim_events WHERE status = 'ACTIVE') AS active_events, "
        "(SELECT count(*) FROM sim_events WHERE status = 'SCHEDULED') AS scheduled_events, "
        "(SELECT count(*) FROM route_snapshots r WHERE r.tick = (SELECT max(tick) FROM sim_ticks) "
        "   AND r.status = 'DISRUPTED') AS disrupted_routes",
        since=(tick["tick"] if tick else 0) - DAY_TICKS,
    )
    ds, ing = await rt.decision_status(), await rt.ingestion_status()
    mode = ds.data.get("mode") if ds.ok else "DEGRADED"
    ing_data = ing.data if ing.ok else {}
    return {
        "tick": tick,
        "kpis": metrics,
        "kpis_prev_day": prev,
        "counts": counts,
        "mode": mode,
        "mode_detail": ds.data if ds.ok else {"error": ds.error},
        "simulator": {
            "state": (ing_data.get("simulator") or {}).get("state", "UNKNOWN") if ing.ok else "UNKNOWN",
            "sse_connected": (ing_data.get("sse") or {}).get("connected") if ing.ok else None,
            "stale": bool(tick and tick["stale"]) or bool(ing_data.get("stale")),
            "sync_lag_ticks": ing_data.get("sync_lag_ticks"),
            "last_sync_age_s": ing_data.get("last_sync_age_s"),
        },
        "demo_controls": rt.s.DEMO_CONTROLS,
        "grafana_url": rt.s.GRAFANA_PUBLIC_URL,
        "simulated_data": True,
    }


@router.get("/metrics/history")
async def metrics_history(request: Request, limit: int = Query(192, ge=2, le=2000)) -> list[dict[str, Any]]:
    rows = await _rt(request).rows(
        "SELECT tick, served, unmet, service_level, allocation_liters, allocation_failures FROM sim_metrics "
        "ORDER BY tick DESC LIMIT :n", n=limit)
    return rows[::-1]


# --------------------------------------------------------------------------------------------------------------------
@router.get("/stations")
async def stations(request: Request) -> list[dict[str, Any]]:
    rt = _rt(request)
    snaps = await rt.rows(
        "SELECT s.* FROM unnest(CAST(:ids AS text[])) AS sid, LATERAL (SELECT station_id, fuel_type, tick, inventory, "
        "capacity, demand_multiplier, status, stale FROM station_snapshots WHERE station_id = sid "
        "ORDER BY tick DESC LIMIT 3) s", ids=list(wc.STATIONS))
    inflight = await rt.rows(
        "SELECT destination_station_id AS sid, fuel_type, sum(quantity) AS qty, count(*) AS n FROM sim_allocations "
        "WHERE status IN ('PENDING','IN_TRANSIT') GROUP BY 1, 2")
    reserved = await rt.rows("SELECT station_id AS sid, fuel_type, sum(quantity) AS qty FROM decisions "
                             "WHERE status = ANY(:st) GROUP BY 1, 2", st=RESERVING)
    routes = await _route_rows(rt)
    fc, fc_err = await _forecast_index(rt)
    inf = {(r["sid"], r["fuel_type"]): r for r in inflight}
    res = {(r["sid"], r["fuel_type"]): float(r["qty"]) for r in reserved}
    route_status = {r["route_id"]: r for r in routes}
    out: dict[str, dict[str, Any]] = {}
    for sn in snaps:
        sid = sn["station_id"]
        st = out.setdefault(sid, {"id": sid, **station_meta(sid), "status": sn["status"],
                                  "demand_multiplier": sn["demand_multiplier"], "tick": sn["tick"],
                                  "stale": sn["stale"], "fuels": {}})
        avail = [route_status[r] for r in st["routes"] if r in route_status and route_status[r]["status"] == "AVAILABLE"]
        lead_min = min((1 + r["transit_ticks"] for r in avail), default=None)
        f = fc.get((sid, sn["fuel_type"]))
        mean0 = f["horizon"][0]["mean"] if f and f.get("horizon") else None
        st["fuels"][sn["fuel_type"]] = {
            "inventory": sn["inventory"],
            "capacity": sn["capacity"],
            "fill": sn["inventory"] / sn["capacity"] if sn["capacity"] else 0.0,
            "in_flight": float(inf.get((sid, sn["fuel_type"]), {}).get("qty") or 0.0),
            "in_flight_count": int(inf.get((sid, sn["fuel_type"]), {}).get("n") or 0),
            "reserved": res.get((sid, sn["fuel_type"]), 0.0),
            "safety_stock": None if mean0 is None or lead_min is None else round(mean0 * (lead_min + 2), 1),
            "burn_rate_lph": f.get("burn_rate_lph") if f else None,
            "t_empty_ticks": f.get("t_empty_ticks") if f else None,
            "t_empty_hours": f.get("t_empty_hours") if f else None,
            "stockout_risk": f.get("stockout_risk") if f else None,
            "forecast_source": f.get("source") if f else None,
        }
        st["available_routes"] = [r["route_id"] for r in avail]
    for st in out.values():
        st["forecast_error"] = fc_err
    return [out[s] for s in wc.STATIONS if s in out]


@router.get("/depots")
async def depots(request: Request) -> list[dict[str, Any]]:
    rt = _rt(request)
    snaps = await rt.rows(
        "SELECT d.* FROM unnest(CAST(:ids AS text[])) AS did, LATERAL (SELECT depot_id, fuel_type, tick, inventory, "
        "capacity, dispatch_capacity_per_tick, status, stale FROM depot_snapshots WHERE depot_id = did "
        "ORDER BY tick DESC LIMIT 3) d", ids=list(wc.DEPOTS))
    pending = await rt.rows("SELECT source_depot_id AS did, fuel_type, sum(quantity) AS qty FROM sim_allocations "
                            "WHERE status = 'PENDING' GROUP BY 1, 2")
    reserved = await rt.rows("SELECT source_depot_id AS did, fuel_type, sum(quantity) AS qty FROM decisions "
                             "WHERE status = ANY(:st) GROUP BY 1, 2", st=RESERVING)
    nxt = await rt.rows("SELECT DISTINCT ON (depot_id, fuel_type) depot_id, fuel_type, planned_tick, quantity, status "
                        "FROM supply_arrivals WHERE status <> 'ARRIVED' ORDER BY depot_id, fuel_type, planned_tick")
    pend = {(r["did"], r["fuel_type"]): float(r["qty"]) for r in pending}
    res = {(r["did"], r["fuel_type"]): float(r["qty"]) for r in reserved}
    nx = {(r["depot_id"], r["fuel_type"]): r for r in nxt}
    out: dict[str, dict[str, Any]] = {}
    for sn in snaps:
        did = sn["depot_id"]
        d = out.setdefault(did, {"id": did, "name": name(did), "region_id": wc.DEPOTS[did]["region_id"],
                                 "status": sn["status"], "dispatch_capacity_per_tick": sn["dispatch_capacity_per_tick"],
                                 "tick": sn["tick"], "stale": sn["stale"],
                                 "routes": [r for r, v in wc.ROUTES.items() if v["depot"] == did], "fuels": {}})
        key = (did, sn["fuel_type"])
        d["fuels"][sn["fuel_type"]] = {
            "inventory": sn["inventory"], "capacity": sn["capacity"],
            "fill": sn["inventory"] / sn["capacity"] if sn["capacity"] else 0.0,
            "pending_out": pend.get(key, 0.0), "reserved": res.get(key, 0.0),
            "next_supply": nx.get(key),
        }
    return [out[d] for d in wc.DEPOTS if d in out]


async def _route_rows(rt: Runtime) -> list[dict[str, Any]]:
    return await rt.rows(
        "SELECT r.* FROM unnest(CAST(:ids AS text[])) AS rid, LATERAL (SELECT route_id, tick, status, transit_ticks, "
        "max_shipment FROM route_snapshots WHERE route_id = rid ORDER BY tick DESC LIMIT 1) r", ids=list(wc.ROUTES))


@router.get("/routes")
async def routes(request: Request) -> list[dict[str, Any]]:
    rt = _rt(request)
    rows = await _route_rows(rt)
    events = await rt.rows("SELECT id, status, start_tick, end_tick, parameters FROM sim_events "
                           "WHERE type = 'route_disruption' AND status IN ('SCHEDULED','ACTIVE')")
    out = []
    for r in rows:
        meta = wc.ROUTES[r["route_id"]]
        hits = [e for e in events if not (e["parameters"] or {}).get("route_ids")
                or r["route_id"] in (e["parameters"] or {}).get("route_ids", [])]
        out.append({**r, "depot_id": meta["depot"], "station_id": meta["station"], "depot_name": name(meta["depot"]),
                    "station_name": name(meta["station"]),
                    "single_route_station": meta["station"] in wc.SINGLE_ROUTE_STATIONS,
                    "disruptions": hits})
    return out


@router.get("/supply")
async def supply(request: Request) -> list[dict[str, Any]]:
    return await _rt(request).rows(
        "SELECT id, depot_id, fuel_type, quantity, first_quantity, planned_tick, first_planned_tick, actual_tick, status "
        "FROM supply_arrivals ORDER BY planned_tick, id")


@router.get("/events")
async def events(request: Request, status: str | None = None) -> list[dict[str, Any]]:
    rt = _rt(request)
    if status:
        return await rt.rows("SELECT * FROM sim_events WHERE status = ANY(:st) ORDER BY id DESC LIMIT 200",
                             st=status.split(","))
    return await rt.rows("SELECT * FROM sim_events ORDER BY id DESC LIMIT 200")


@router.get("/allocations")
async def allocations(request: Request, status: str | None = None,
                      limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
    rt = _rt(request)
    q = ("SELECT a.*, d.decision_id::text AS decision_id, d.system_origin FROM sim_allocations a "
         "LEFT JOIN decisions d ON d.idempotency_key = a.idempotency_key")
    params: dict[str, Any] = {"n": limit}
    if status:
        q += " WHERE a.status = ANY(:st)"
        params["st"] = status.split(",")
    return await rt.rows(q + " ORDER BY a.id DESC LIMIT :n", **params)


@router.get("/alerts")
async def alerts(request: Request, limit: int = Query(100, ge=1, le=500),
                 open_only: bool = False) -> list[dict[str, Any]]:
    q = "SELECT * FROM alerts"
    if open_only:
        q += " WHERE NOT coalesce(acknowledged, false)"
    return await _rt(request).rows(q + " ORDER BY id DESC LIMIT :n", n=limit)


@router.post("/alerts/{alert_id}/ack")
async def ack_alert(alert_id: int, request: Request) -> dict[str, Any]:
    rt = _rt(request)
    async with rt.engine.begin() as c:
        n = (await c.execute(text("UPDATE alerts SET acknowledged = true WHERE id = :i"), {"i": alert_id})).rowcount
    if not n:
        raise HTTPException(404, "alert not found")
    return {"id": alert_id, "acknowledged": True}


@router.post("/alerts/ack-all")
async def ack_all(request: Request) -> dict[str, Any]:
    rt = _rt(request)
    async with rt.engine.begin() as c:
        n = (await c.execute(text("UPDATE alerts SET acknowledged = true WHERE NOT coalesce(acknowledged,false)"))).rowcount
    await rt.audit(actor=request.headers.get("x-operator", "operator"), action="alerts.ack_all", entity_type="alert",
                   entity_id=None, result="OK", data={"count": n})
    return {"acknowledged": n}


@router.get("/incidents")
async def incidents(request: Request, limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    return await _rt(request).rows(
        "SELECT i.id, i.sim_event_id, i.brief, i.source, i.created_at, e.type AS event_type, e.status AS event_status "
        "FROM incidents i LEFT JOIN sim_events e ON e.id = i.sim_event_id ORDER BY i.id DESC LIMIT :n", n=limit)


# --------------------------------------------------------------------------------------------------------------------
@router.get("/forecasts")
async def forecasts(request: Request, station_id: str | None = None, fuel_type: str | None = None,
                    history: int = Query(64, ge=0, le=500)) -> dict[str, Any]:
    """forecast-svc horizon (+ observed demand history for the chart). Without filters: all 12 pairs, no history."""
    rt = _rt(request)
    up = await rt.forecasts()
    if not up.ok:
        return {"available": False, "error": up.error, "forecasts": [], "observed": []}
    fc = [f for f in up.data.get("forecasts", [])
          if (station_id is None or f["station_id"] == station_id) and (fuel_type is None or f["fuel_type"] == fuel_type)]
    observed: list[dict[str, Any]] = []
    if station_id and fuel_type and history:
        observed = (await rt.rows(
            "SELECT tick, sim_time, demand_liters, served_liters, unmet_liters FROM demand_observations "
            "WHERE station_id = :s AND fuel_type = :f ORDER BY tick DESC LIMIT :n",
            s=station_id, f=fuel_type, n=history))[::-1]
    return {"available": True, "tick": up.data.get("tick"), "model_version": up.data.get("model_version"),
            "forecasts": fc, "observed": observed}
