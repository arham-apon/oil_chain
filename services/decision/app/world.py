"""Freshest state decision-svc plans on, loaded from Postgres (written by ingestion-svc).

``World`` is a plain, copyable snapshot so the planner, validator and what-if code are pure functions of it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fsp_shared import world_constants as wc
from fsp_shared.demand import EventSpec
from fsp_shared.timeutil import parse_sim_time
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

FUELS = wc.FUEL_TYPES
RESERVING_STATUSES = ("STAGED_REVIEW", "AUTO_APPROVED", "OPERATOR_APPROVED", "COMMITTING")  # not yet a sim allocation


@dataclass
class DepotView:
    id: str
    region_id: str
    status: str
    dispatch_capacity: float
    inventory: dict[str, float]
    capacity: dict[str, float]


@dataclass
class StationView:
    id: str
    region_id: str
    status: str
    multiplier: float
    inventory: dict[str, float]
    capacity: dict[str, float]


@dataclass
class RouteView:
    id: str
    depot_id: str
    station_id: str
    transit_ticks: int
    max_shipment: float
    status: str


@dataclass
class AllocView:
    id: int
    key: str
    depot_id: str
    station_id: str
    route_id: str
    fuel: str
    qty: float
    created_tick: int | None
    departure_tick: int | None
    expected_arrival_tick: int | None
    status: str


@dataclass
class ReservedView:
    """A decision that holds quantity but is not yet a simulator allocation."""

    decision_id: str
    depot_id: str
    station_id: str
    route_id: str
    fuel: str
    qty: float
    status: str


@dataclass
class SupplyView:
    id: str
    depot_id: str
    fuel: str
    qty: float
    first_qty: float
    planned_tick: int
    first_planned_tick: int
    status: str


@dataclass
class World:
    tick: int
    sim_time: datetime
    tick_minutes: int
    sim_status: str
    stale: bool
    depots: dict[str, DepotView]
    stations: dict[str, StationView]
    routes: dict[str, RouteView]
    events: list[EventSpec]
    allocations: list[AllocView]
    reserved: list[ReservedView]
    supply: list[SupplyView]
    loaded_at: float = 0.0
    overrides: dict[str, Any] = field(default_factory=dict)

    def copy(self) -> World:
        return copy.deepcopy(self)

    # --- helpers used by planner / validator ------------------------------------------------------------------------
    def in_flight(self) -> list[AllocView]:
        return [a for a in self.allocations if a.status in ("PENDING", "IN_TRANSIT")]

    def station_in_flight(self, station_id: str, fuel: str) -> float:
        """Sim PENDING + IN_TRANSIT plus reserved (staged / approved / committing) — the ``F(s,f)`` of spec §11.2."""
        sim = sum(a.qty for a in self.in_flight() if a.station_id == station_id and a.fuel == fuel)
        res = sum(r.qty for r in self.reserved if r.station_id == station_id and r.fuel == fuel)
        return sim + res

    def depot_pending(self, depot_id: str, fuel: str) -> float:
        """Stock already spoken for at ``depot_id``: sim PENDING (deducted on creation) is already out of the
        inventory we read, so only decisions not yet committed reduce the usable stock (``U`` of spec §11.2)."""
        return sum(r.qty for r in self.reserved if r.depot_id == depot_id and r.fuel == fuel)

    def dispatch_used(self, depot_id: str) -> float:
        """Sim allocations from this depot that count against this tick's dispatch capacity (created or departing now)."""
        return sum(
            a.qty
            for a in self.in_flight()
            if a.depot_id == depot_id and (a.created_tick == self.tick or a.departure_tick == self.tick)
        )

    def route_ids_from(self, depot_id: str) -> list[str]:
        return [r.id for r in self.routes.values() if r.depot_id == depot_id]

    def routes_to(self, station_id: str) -> list[RouteView]:
        return [r for r in self.routes.values() if r.station_id == station_id]


_Q_TICK = "SELECT tick, sim_time, status, tick_minutes, stale FROM sim_ticks ORDER BY tick DESC LIMIT 1"
_Q_DEPOTS = """SELECT DISTINCT ON (depot_id, fuel_type) depot_id, fuel_type, inventory, capacity,
       dispatch_capacity_per_tick, status FROM depot_snapshots ORDER BY depot_id, fuel_type, tick DESC"""
_Q_STATIONS = """SELECT DISTINCT ON (station_id, fuel_type) station_id, fuel_type, inventory, capacity,
       demand_multiplier, status FROM station_snapshots ORDER BY station_id, fuel_type, tick DESC"""
_Q_ROUTES = """SELECT DISTINCT ON (route_id) route_id, status, transit_ticks, max_shipment
       FROM route_snapshots ORDER BY route_id, tick DESC"""
_Q_EVENTS = "SELECT type, start_tick, end_tick, status, parameters FROM sim_events WHERE status <> 'RESOLVED' OR end_tick >= :cut"
_Q_ALLOCS = """SELECT id, idempotency_key, source_depot_id, destination_station_id, route_id, fuel_type, quantity,
       created_tick, departure_tick, expected_arrival_tick, status FROM sim_allocations
       WHERE status IN ('PENDING', 'IN_TRANSIT')"""
_Q_RESERVED = """SELECT decision_id::text, source_depot_id, station_id, route_id, fuel_type, quantity, status
       FROM decisions WHERE status = ANY(:st) AND sim_allocation_id IS NULL"""
_Q_SUPPLY = """SELECT id, depot_id, fuel_type, quantity, coalesce(first_quantity, quantity), planned_tick,
       coalesce(first_planned_tick, planned_tick), status FROM supply_arrivals"""


async def load_world(engine: AsyncEngine) -> World | None:
    import time

    async with engine.connect() as c:
        t = (await c.execute(text(_Q_TICK))).first()
        if t is None:
            return None
        tick = int(t.tick)
        depots_r = (await c.execute(text(_Q_DEPOTS))).all()
        stations_r = (await c.execute(text(_Q_STATIONS))).all()
        routes_r = (await c.execute(text(_Q_ROUTES))).all()
        events_r = (await c.execute(text(_Q_EVENTS), {"cut": tick - 8})).all()
        allocs_r = (await c.execute(text(_Q_ALLOCS))).all()
        reserved_r = (await c.execute(text(_Q_RESERVED), {"st": list(RESERVING_STATUSES)})).all()
        supply_r = (await c.execute(text(_Q_SUPPLY))).all()

    depots: dict[str, DepotView] = {}
    for r in depots_r:
        d = depots.setdefault(
            r.depot_id,
            DepotView(r.depot_id, wc.DEPOTS.get(r.depot_id, {}).get("region_id", ""), r.status,
                      float(r.dispatch_capacity_per_tick), {}, {}),
        )
        d.inventory[r.fuel_type] = float(r.inventory)
        d.capacity[r.fuel_type] = float(r.capacity)
    stations: dict[str, StationView] = {}
    for r in stations_r:
        s = stations.setdefault(
            r.station_id,
            StationView(r.station_id, wc.STATIONS.get(r.station_id, {}).get("region_id", ""), r.status,
                        float(r.demand_multiplier), {}, {}),
        )
        s.inventory[r.fuel_type] = float(r.inventory)
        s.capacity[r.fuel_type] = float(r.capacity)
    routes = {
        r.route_id: RouteView(
            r.route_id,
            wc.ROUTES.get(r.route_id, {}).get("depot", ""),
            wc.ROUTES.get(r.route_id, {}).get("station", ""),
            int(r.transit_ticks),
            float(r.max_shipment),
            r.status,
        )
        for r in routes_r
    }
    return World(
        tick=tick,
        sim_time=parse_sim_time(t.sim_time),
        tick_minutes=int(t.tick_minutes),
        sim_status=str(t.status),
        stale=bool(t.stale),
        depots=depots,
        stations=stations,
        routes=routes,
        events=[EventSpec(e.type, int(e.start_tick), int(e.end_tick), e.parameters or {}, e.status) for e in events_r],
        allocations=[
            AllocView(a.id, a.idempotency_key, a.source_depot_id, a.destination_station_id, a.route_id, a.fuel_type,
                      float(a.quantity), a.created_tick, a.departure_tick, a.expected_arrival_tick, a.status)
            for a in allocs_r
        ],
        reserved=[ReservedView(r[0], r[1], r[2], r[3], r[4], float(r[5]), r[6]) for r in reserved_r],
        supply=[SupplyView(r[0], r[1], r[2], float(r[3]), float(r[4]), int(r[5]), int(r[6]), r[7]) for r in supply_r],
        loaded_at=time.monotonic(),
    )
