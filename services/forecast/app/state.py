"""In-memory snapshot of the world that ``/forecast`` computes from.

A background task refreshes it from Postgres (written by ingestion-svc); request handlers never touch the database,
which is what keeps ``/forecast`` for all 12 pairs well under 30 ms.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from fsp_shared import world_constants as wc
from fsp_shared.demand import EventSpec
from fsp_shared.timeutil import parse_sim_time
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

TAIL_ROWS = 96  # demand rows kept per (station, fuel) for lag features (EWMA-16 is fully warm after ~64)


@dataclass
class PairState:
    station_id: str
    fuel_type: str
    inventory: float | None
    capacity: float | None
    station_status: str | None
    stale: bool
    demand_tail: np.ndarray  # oldest -> newest
    tail_ticks: np.ndarray  # tick of each tail row (to normalise by the multiplier that applied then)
    n_rows: int  # total observations available (cold-start check)


@dataclass
class InFlight:
    station_id: str
    fuel_type: str
    arrival_tick: int
    quantity: float


@dataclass
class WorldState:
    tick: int
    sim_time: datetime
    tick_minutes: int
    sim_status: str
    stale: bool
    pairs: dict[tuple[str, str], PairState]
    events: list[EventSpec]
    in_flight: list[InFlight]
    loaded_at: float = field(default_factory=time.monotonic)
    version: int = 0

    def age_s(self) -> float:
        return time.monotonic() - self.loaded_at

    def in_flight_for(self, station_id: str, fuel_type: str) -> list[InFlight]:
        return [a for a in self.in_flight if a.station_id == station_id and a.fuel_type == fuel_type]


_TICK = "SELECT tick, sim_time, status, tick_minutes, stale FROM sim_ticks ORDER BY tick DESC LIMIT 1"
_SNAP = """
SELECT DISTINCT ON (station_id, fuel_type) station_id, fuel_type, inventory, capacity, status, stale
FROM station_snapshots ORDER BY station_id, fuel_type, tick DESC"""
_TAIL = """
SELECT station_id, fuel_type, tick, demand_liters FROM (
  SELECT station_id, fuel_type, tick, demand_liters,
         row_number() OVER (PARTITION BY station_id, fuel_type ORDER BY tick DESC) AS rn
  FROM demand_observations WHERE tick > :cut) t
WHERE rn <= :n ORDER BY station_id, fuel_type, tick"""
_COUNTS = "SELECT station_id, fuel_type, count(*) FROM demand_observations GROUP BY station_id, fuel_type"
_EVENTS = """
SELECT type, start_tick, end_tick, status, parameters FROM sim_events
WHERE status <> 'RESOLVED' OR end_tick >= :cut"""
_ALLOCS = """
SELECT destination_station_id, fuel_type, quantity, created_tick, expected_arrival_tick, route_id
FROM sim_allocations WHERE status IN ('PENDING', 'IN_TRANSIT')"""
_ROUTES = "SELECT DISTINCT ON (route_id) route_id, transit_ticks FROM route_snapshots ORDER BY route_id, tick DESC"


class StateCache:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.world: WorldState | None = None
        self._version = 0

    async def refresh(self) -> WorldState | None:
        async with self.engine.connect() as conn:
            tick_row = (await conn.execute(text(_TICK))).first()
            if tick_row is None:
                return None
            tick = int(tick_row.tick)
            snaps = (await conn.execute(text(_SNAP))).all()
            tails = (await conn.execute(text(_TAIL), {"cut": tick - 4 * TAIL_ROWS, "n": TAIL_ROWS})).all()
            counts = {(r[0], r[1]): int(r[2]) for r in (await conn.execute(text(_COUNTS))).all()}
            events = (await conn.execute(text(_EVENTS), {"cut": tick - 2 * TAIL_ROWS})).all()
            allocs = (await conn.execute(text(_ALLOCS))).all()
            routes = {r[0]: int(r[1]) for r in (await conn.execute(text(_ROUTES))).all()}

        tail_map: dict[tuple[str, str], list[tuple[int, float]]] = {}
        for r in tails:
            tail_map.setdefault((r.station_id, r.fuel_type), []).append((int(r.tick), float(r.demand_liters)))
        snap_map = {(r.station_id, r.fuel_type): r for r in snaps}

        pairs: dict[tuple[str, str], PairState] = {}
        for station_id in wc.STATIONS:
            for fuel in wc.FUEL_TYPES:
                snap = snap_map.get((station_id, fuel))
                pairs[(station_id, fuel)] = PairState(
                    station_id=station_id,
                    fuel_type=fuel,
                    inventory=None if snap is None else float(snap.inventory),
                    capacity=None if snap is None else float(snap.capacity),
                    station_status=None if snap is None else snap.status,
                    stale=bool(tick_row.stale) or bool(snap is not None and snap.stale),
                    demand_tail=np.array([d for _, d in tail_map.get((station_id, fuel), [])], dtype=float),
                    tail_ticks=np.array([t for t, _ in tail_map.get((station_id, fuel), [])], dtype=int),
                    n_rows=counts.get((station_id, fuel), 0),
                )

        in_flight = []
        for a in allocs:
            if a.expected_arrival_tick is not None:
                arrival = int(a.expected_arrival_tick)
            elif a.created_tick is not None:  # not departed yet: created + 1 (departure) + transit (spec §10.5)
                transit = routes.get(a.route_id) or int(wc.ROUTES.get(a.route_id, {}).get("transit_ticks", 0))
                arrival = int(a.created_tick) + 1 + transit
            else:
                continue
            in_flight.append(InFlight(a.destination_station_id, a.fuel_type, arrival, float(a.quantity or 0.0)))

        self._version += 1
        self.world = WorldState(
            tick=tick,
            sim_time=parse_sim_time(tick_row.sim_time),
            tick_minutes=int(tick_row.tick_minutes),
            sim_status=str(tick_row.status),
            stale=bool(tick_row.stale),
            pairs=pairs,
            events=[
                EventSpec(
                    type=e.type,
                    start_tick=int(e.start_tick),
                    end_tick=int(e.end_tick),
                    parameters=e.parameters or {},
                    status=e.status,
                )
                for e in events
            ],
            in_flight=in_flight,
            version=self._version,
        )
        return self.world

    def info(self) -> dict[str, Any]:
        w = self.world
        if w is None:
            return {"loaded": False}
        return {"loaded": True, "tick": w.tick, "age_s": round(w.age_s(), 2), "version": w.version, "stale": w.stale}
