"""Postgres persistence for ingestion-svc: idempotent upserts, alerts, reset wipe and retention."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from fsp_shared.models import (
    Alert,
    DemandObservation,
    DepotSnapshot,
    Forecast,
    RouteSnapshot,
    SimAllocation,
    SimEventRow,
    SimMetric,
    SimTick,
    StationSnapshot,
    SupplyArrivalRow,
)
from fsp_shared.schemas import (
    Allocation,
    DemandRow,
    Depot,
    FuelType,
    Instance,
    Metrics,
    Route,
    SimEvent,
    Station,
    SupplyArrival,
)
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

FUELS: tuple[FuelType, ...] = ("DIESEL", "PETROL", "OCTANE")

# Mirror tables wiped when the simulator is reset. Decisions, audit_log, alerts, incidents, ai_calls and
# model_registry are never deleted (spec §8: "never delete decisions/audit").
RESET_TABLES = (
    SimTick,
    DepotSnapshot,
    StationSnapshot,
    RouteSnapshot,
    SupplyArrivalRow,
    SimEventRow,
    DemandObservation,
    SimAllocation,
    SimMetric,
    Forecast,
)


async def _upsert(
    session: AsyncSession, model: Any, rows: Sequence[dict[str, Any]], keys: Sequence[str]
) -> None:
    if not rows:
        return
    stmt = pg_insert(model).values(list(rows))
    update_cols = {c: stmt.excluded[c] for c in rows[0] if c not in keys}
    if update_cols:
        stmt = stmt.on_conflict_do_update(index_elements=list(keys), set_=update_cols)
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=list(keys))
    await session.execute(stmt)


async def upsert_tick(session: AsyncSession, inst: Instance, stale: bool) -> None:
    await _upsert(
        session,
        SimTick,
        [
            {
                "tick": inst.tick,
                "sim_time": inst.sim_time,
                "status": inst.status,
                "tick_minutes": inst.tick_minutes,
                "stale": stale,
            }
        ],
        ["tick"],
    )


async def upsert_depots(session: AsyncSession, tick: int, depots: Iterable[Depot], stale: bool) -> None:
    rows = [
        {
            "tick": tick,
            "depot_id": d.id,
            "fuel_type": fuel,
            "inventory": d.inventory[fuel],
            "capacity": d.capacity[fuel],
            "dispatch_capacity_per_tick": d.dispatch_capacity_per_tick,
            "status": d.status,
            "stale": stale,
        }
        for d in depots
        for fuel in FUELS
    ]
    await _upsert(session, DepotSnapshot, rows, ["depot_id", "fuel_type", "tick"])


async def upsert_stations(session: AsyncSession, tick: int, stations: Iterable[Station], stale: bool) -> None:
    rows = [
        {
            "tick": tick,
            "station_id": s.id,
            "fuel_type": fuel,
            "inventory": s.inventory[fuel],
            "capacity": s.capacity[fuel],
            "demand_multiplier": s.demand_multiplier,
            "status": s.status,
            "stale": stale,
        }
        for s in stations
        for fuel in FUELS
    ]
    await _upsert(session, StationSnapshot, rows, ["station_id", "fuel_type", "tick"])


async def upsert_routes(session: AsyncSession, tick: int, routes: Iterable[Route]) -> None:
    rows = [
        {
            "tick": tick,
            "route_id": r.id,
            "status": r.status,
            "transit_ticks": r.transit_ticks,
            "max_shipment": r.max_shipment,
        }
        for r in routes
    ]
    await _upsert(session, RouteSnapshot, rows, ["route_id", "tick"])


async def upsert_events(session: AsyncSession, tick: int, events: Iterable[SimEvent]) -> None:
    """``first_seen_tick`` is set on insert and never overwritten."""
    rows = [
        {
            "id": e.id,
            "type": e.type,
            "start_tick": e.start_tick,
            "end_tick": e.end_tick,
            "status": e.status,
            "parameters": e.parameters,
            "first_seen_tick": tick,
        }
        for e in events
    ]
    if not rows:
        return
    stmt = pg_insert(SimEventRow).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "type": stmt.excluded.type,
            "start_tick": stmt.excluded.start_tick,
            "end_tick": stmt.excluded.end_tick,
            "status": stmt.excluded.status,
            "parameters": stmt.excluded.parameters,
            "updated_at": func.now(),  # type: ignore[dict-item]
        },
    )
    await session.execute(stmt)


async def upsert_allocations(session: AsyncSession, allocations: Iterable[Allocation]) -> None:
    rows = [
        {
            "id": a.id,
            "idempotency_key": a.idempotency_key,
            "source_depot_id": a.source_depot_id,
            "destination_station_id": a.destination_station_id,
            "route_id": a.route_id,
            "fuel_type": a.fuel_type,
            "quantity": a.quantity,
            "created_tick": a.created_tick,
            "departure_tick": a.departure_tick,
            "expected_arrival_tick": a.expected_arrival_tick,
            "actual_arrival_tick": a.actual_arrival_tick,
            "status": a.status,
            "failure_reason": a.failure_reason,
        }
        for a in allocations
    ]
    if not rows:
        return
    stmt = pg_insert(SimAllocation).values(rows)
    update_cols = {c: stmt.excluded[c] for c in rows[0] if c != "id"}
    update_cols["updated_at"] = func.now()  # type: ignore[assignment]
    await session.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=update_cols))


async def upsert_metrics(session: AsyncSession, tick: int, metrics: Metrics) -> None:
    await _upsert(
        session,
        SimMetric,
        [
            {
                "tick": tick,
                "served": metrics.served_demand_liters,
                "unmet": metrics.unmet_demand_liters,
                "service_level": metrics.service_level,
                "allocation_liters": metrics.allocation_liters,
                "allocation_failures": metrics.allocation_failures,
            }
        ],
        ["tick"],
    )


async def upsert_supply(session: AsyncSession, arrivals: Iterable[SupplyArrival]) -> None:
    rows = [
        {
            "id": a.id,
            "depot_id": a.depot_id,
            "fuel_type": a.fuel_type,
            "quantity": a.quantity,
            "planned_tick": a.planned_tick,
            "actual_tick": a.actual_tick,
            "status": a.status,
            # first-seen values are set on insert only (never in update_cols): shortfall/delay detection needs them
            "first_quantity": a.quantity,
            "first_planned_tick": a.planned_tick,
        }
        for a in arrivals
    ]
    if not rows:
        return
    stmt = pg_insert(SupplyArrivalRow).values(rows)
    update_cols = {c: stmt.excluded[c] for c in rows[0] if c not in ("id", "first_quantity", "first_planned_tick")}
    update_cols["updated_at"] = func.now()  # type: ignore[assignment]
    await session.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=update_cols))


async def insert_demand(session: AsyncSession, rows: Iterable[DemandRow]) -> int:
    """Demand history is immutable: conflicts are ignored. Returns the highest tick seen (or -1)."""
    rows_list = list(rows)
    values = [
        {
            "id": r.id,
            "station_id": r.station_id,
            "fuel_type": r.fuel_type,
            "tick": r.tick,
            "sim_time": r.sim_time,
            "demand_liters": r.demand_liters,
            "served_liters": r.served_liters,
            "unmet_liters": r.unmet_liters,
        }
        for r in rows_list
    ]
    if not values:
        return -1
    await session.execute(pg_insert(DemandObservation).values(values).on_conflict_do_nothing())
    return max(r.tick for r in rows_list)


async def demand_gaps(session: AsyncSession) -> dict[str, int]:
    """Missing (station, fuel, tick) rows between each series' first and last tick. Empty dict = no gaps."""
    stmt = select(
        DemandObservation.station_id,
        DemandObservation.fuel_type,
        func.min(DemandObservation.tick),
        func.max(DemandObservation.tick),
        func.count(),
    ).group_by(DemandObservation.station_id, DemandObservation.fuel_type)
    gaps: dict[str, int] = {}
    for station, fuel, lo, hi, n in (await session.execute(stmt)).all():
        missing = (hi - lo + 1) - n
        if missing:
            gaps[f"{station}/{fuel}"] = missing
    return gaps


async def max_demand_tick(session: AsyncSession) -> int | None:
    return (await session.execute(select(func.max(DemandObservation.tick)))).scalar_one()


async def insert_alert(
    session: AsyncSession,
    *,
    tick: int | None,
    kind: str,
    severity: str,
    message: str,
    entity_id: str | None = None,
    fuel_type: str | None = None,
    data: dict[str, Any] | None = None,
) -> int:
    alert = Alert(
        tick=tick,
        kind=kind,
        severity=severity,
        entity_id=entity_id,
        fuel_type=fuel_type,
        message=message,
        data=data,
    )
    session.add(alert)
    await session.flush()
    return alert.id


async def wipe_mirror_tables(session: AsyncSession) -> None:
    for model in RESET_TABLES:
        await session.execute(delete(model))


async def apply_retention(session: AsyncSession, current_tick: int, keep_ticks: int) -> int:
    """Delete snapshot rows older than ``keep_ticks``. Never touches decisions / audit / alerts."""
    cutoff = current_tick - keep_ticks
    if cutoff <= 0:
        return 0
    deleted = 0
    for model in (DepotSnapshot, StationSnapshot, RouteSnapshot, SimTick, SimMetric):
        res = await session.execute(delete(model).where(model.tick < cutoff))
        deleted += getattr(res, "rowcount", 0) or 0
    return deleted


async def latest_tick(session: AsyncSession) -> int | None:
    return (await session.execute(text("SELECT max(tick) FROM sim_ticks"))).scalar_one()
