import copy

import httpx
import pytest
from fsp_shared.models import (
    Alert,
    Decision,
    DemandObservation,
    DepotSnapshot,
    RouteSnapshot,
    SimAllocation,
    SimEventRow,
    SimMetric,
    SimTick,
    StationSnapshot,
    SupplyArrivalRow,
)
from prometheus_client import REGISTRY
from sqlalchemy import func, select

from app import store
from app.demand_backfill import incremental_limit
from app.state import SafeBus
from app.sync import SyncError

from .conftest import bus_types
from .helpers import STATION_IDS, allocation, demand_rows, make_world


async def count(factory, model, *where):
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


def counter(name, labels):
    return REGISTRY.get_sample_value(name, labels) or 0


# --- persistence --------------------------------------------------------------------------------
async def test_tick_sync_persists_everything_and_publishes(syncer, sim, redis, state):
    sim.world["events"] = [
        {
            "id": 1,
            "type": "demand_spike",
            "start_tick": 8,
            "end_tick": 20,
            "status": "SCHEDULED",
            "parameters": {"multiplier": 1.8},
        }
    ]
    sim.world["allocations"] = [allocation(1)]
    assert await syncer.tick_sync() == 5

    f = syncer.factory
    assert await count(f, SimTick) == 1
    assert await count(f, DepotSnapshot) == 6  # 2 depots x 3 fuels
    assert await count(f, StationSnapshot) == 12  # 4 stations x 3 fuels
    assert await count(f, RouteSnapshot) == 2
    assert (
        await count(f, SimEventRow) == 1
        and await count(f, SimAllocation) == 1
        and await count(f, SimMetric) == 1
    )
    async with f() as s:
        snap = (
            await s.execute(
                select(StationSnapshot).where(
                    StationSnapshot.station_id == "station-mirpur", StationSnapshot.fuel_type == "DIESEL"
                )
            )
        ).scalar_one()
        assert (snap.tick, snap.inventory, snap.capacity, snap.stale) == (5, 8995, 15000, False)
        ev = (await s.execute(select(SimEventRow))).scalar_one()
        assert ev.first_seen_tick == 5 and ev.parameters == {"multiplier": 1.8}
    assert state.last_synced_tick == 5 and state.station_ids == list(STATION_IDS) and state.tick_minutes == 15
    assert state.last_good["tick"] == 5
    types = [t for t, _ in await bus_types(redis)]
    assert types == ["state.updated"]  # first population publishes no event/allocation "changes"
    assert (await bus_types(redis))[0][1] == {"tick": 5, "stale": False}


async def test_repeated_sync_of_same_tick_is_idempotent_and_overwrites(syncer, sim):
    await syncer.tick_sync()
    sim.world["stations"][0]["inventory"]["DIESEL"] = 1234.0
    await syncer.tick_sync()
    f = syncer.factory
    assert await count(f, StationSnapshot) == 12 and await count(f, SimTick) == 1
    async with f() as s:
        inv = (
            await s.execute(
                select(StationSnapshot.inventory).where(
                    StationSnapshot.station_id == "station-mirpur", StationSnapshot.fuel_type == "DIESEL"
                )
            )
        ).scalar_one()
    assert inv == 1234.0


async def test_ticks_accumulate_and_first_seen_tick_is_preserved(syncer, sim):
    sim.world["events"] = [
        {
            "id": 1,
            "type": "route_disruption",
            "start_tick": 6,
            "end_tick": 9,
            "status": "SCHEDULED",
            "parameters": {},
        }
    ]
    await syncer.tick_sync()
    for tick, status in ((6, "ACTIVE"), (10, "RESOLVED")):
        sim.world = make_world(tick)
        sim.world["events"] = [
            {
                "id": 1,
                "type": "route_disruption",
                "start_tick": 6,
                "end_tick": 9,
                "status": status,
                "parameters": {},
            }
        ]
        await syncer.tick_sync()
    f = syncer.factory
    assert await count(f, SimTick) == 3
    async with f() as s:
        ev = (await s.execute(select(SimEventRow))).scalar_one()
    assert ev.status == "RESOLVED" and ev.first_seen_tick == 5


# --- change events ------------------------------------------------------------------------------------
async def test_event_and_allocation_changes_are_published(syncer, sim, redis):
    await syncer.tick_sync()  # primes caches silently
    sim.world = make_world(6)
    sim.world["events"] = [
        {
            "id": 1,
            "type": "route_disruption",
            "start_tick": 8,
            "end_tick": 12,
            "status": "SCHEDULED",
            "parameters": {"route_ids": ["route-gazipur-mirpur"]},
        }
    ]
    sim.world["allocations"] = [allocation(1, "PENDING")]
    await syncer.tick_sync()
    sim.world = make_world(8)
    sim.world["events"][:] = [
        {
            "id": 1,
            "type": "route_disruption",
            "start_tick": 8,
            "end_tick": 12,
            "status": "ACTIVE",
            "parameters": {},
        }
    ]
    sim.world["allocations"] = [allocation(1, "FAILED", failure_reason="route disrupted")]
    await syncer.tick_sync()
    sim.world = make_world(9)
    sim.world["events"] = [
        {
            "id": 1,
            "type": "route_disruption",
            "start_tick": 8,
            "end_tick": 12,
            "status": "ACTIVE",
            "parameters": {},
        }
    ]
    sim.world["allocations"] = [allocation(1, "FAILED", failure_reason="route disrupted")]
    await syncer.tick_sync()  # nothing changed -> no new change events

    published = await bus_types(redis)
    ev = [p for t, p in published if t == "event.changed"]
    al = [p for t, p in published if t == "allocation.changed"]
    assert [(e["id"], e["change"], e["status"]) for e in ev] == [
        (1, "new", "SCHEDULED"),
        (1, "status", "ACTIVE"),
    ]
    assert [(a["id"], a["status"]) for a in al] == [(1, "PENDING"), (1, "FAILED")]
    assert al[1]["failure_reason"] == "route disrupted"
    assert sum(1 for t, _ in published if t == "state.updated") == 4


# --- stale data ------------------------------------------------------------------------------------------
async def test_stale_data_flags_rows_alerts_and_publishes_once(syncer, sim, redis, state):
    await syncer.tick_sync()
    good = state.last_good["tick"]
    sim.headers = {"X-Simulator-Stale": "true"}
    sim.world = make_world(7)
    await syncer.tick_sync()
    sim.world = make_world(8)
    await syncer.tick_sync()  # still stale: no second alert

    f = syncer.factory
    assert state.stale is True and state.stale_since_tick == 7
    assert state.last_good["tick"] == good == 5  # last non-stale payloads are retained
    assert await count(f, StationSnapshot, StationSnapshot.stale.is_(True)) == 24  # ticks 7 and 8
    assert await count(f, StationSnapshot, StationSnapshot.stale.is_(False)) == 12
    assert await count(f, SimTick, SimTick.stale.is_(True)) == 2
    assert await count(f, Alert, Alert.kind == "STALE_DATA") == 1
    faults = [p for t, p in await bus_types(redis) if t == "sim.fault"]
    assert faults == [{"type": "stale_data", "active": True, "tick": 7}]
    assert [p for t, p in await bus_types(redis) if t == "state.updated"][-1] == {"tick": 8, "stale": True}

    sim.headers = {}
    sim.world = make_world(9)
    await syncer.tick_sync()
    assert state.stale is False and state.last_good["tick"] == 9
    faults = [p for t, p in await bus_types(redis) if t == "sim.fault"]
    assert faults[-1] == {"type": "stale_data", "active": False, "tick": 9}
    assert REGISTRY.get_sample_value("sim_stale") == 0


# --- malformed data ---------------------------------------------------------------------------------------
async def test_invalid_core_payload_is_rejected_keeps_last_valid_and_alerts(syncer, sim, state):
    await syncer.tick_sync()
    before = counter("sim_payload_invalid_total", {"endpoint": "/v1/stations"})
    bad = copy.deepcopy(make_world(6))
    bad["stations"][0]["inventory"]["DIESEL"] = -50  # negative volume
    sim.world = bad
    with pytest.raises(SyncError):
        await syncer.tick_sync()
    with pytest.raises(SyncError):
        await syncer.tick_sync()  # second failure: metric counts, alert is rate-limited

    f = syncer.factory
    assert state.last_synced_tick == 5 and state.last_good["tick"] == 5  # cache untouched
    assert await count(f, SimTick) == 1 and await count(f, StationSnapshot) == 12  # nothing partial written
    assert await count(f, Alert, Alert.kind == "DATA_INVALID") == 1
    assert counter("sim_payload_invalid_total", {"endpoint": "/v1/stations"}) == before + 2
    assert state.sync_failures == 2


async def test_missing_fuel_key_and_wrong_enum_are_invalid(syncer, sim):
    w = make_world(6)
    del w["depots"][0]["inventory"]["OCTANE"]
    sim.world = w
    with pytest.raises(SyncError):
        await syncer.tick_sync()
    w = make_world(6)
    w["routes"][0]["status"] = "EXPLODED"
    sim.world = w
    with pytest.raises(SyncError):
        await syncer.tick_sync()


async def test_invalid_optional_payload_syncs_partially(syncer, sim):
    sim.world["events"] = [{"id": "not-an-int", "type": "x"}]
    assert await syncer.tick_sync() == 5
    f = syncer.factory
    assert await count(f, SimTick) == 1 and await count(f, SimEventRow) == 0
    assert await count(f, Alert, Alert.entity_id == "/v1/events") == 1


async def test_core_endpoint_503_raises_sync_error_and_writes_nothing(syncer, sim):
    sim.overrides["depots"] = httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}})
    with pytest.raises(SyncError, match="depots"):
        await syncer.tick_sync()
    assert await count(syncer.factory, SimTick) == 0


# --- reset ---------------------------------------------------------------------------------------------------
async def test_tick_regression_wipes_mirror_tables_but_keeps_decisions(syncer, sim, redis, state):
    import uuid

    sim.world = make_world(50)
    sim.world["allocations"] = [allocation(1)]
    await syncer.tick_sync()
    did = uuid.uuid4()
    async with syncer.factory() as s:
        s.add(
            Decision(
                decision_id=did,
                idempotency_key=f"fsp-{did}",
                cycle_tick=50,
                quantity=500.0,
                system_origin="SYSTEM1_AUTO",
                status="COMMITTED",
                facts={},
            )
        )
        await s.commit()

    sim.world = make_world(1)  # simulator was reset behind our back
    await syncer.tick_sync()
    f = syncer.factory
    assert await count(f, SimTick) == 1 and syncer.state.last_synced_tick == 1
    assert await count(f, SimAllocation) == 0
    assert await count(f, Decision) == 1
    assert "sim.reset" in [t for t, _ in await bus_types(redis)]
    assert state.needs_backfill is True


async def test_explicit_reset_notice(syncer, sim, redis, state):
    await syncer.tick_sync()
    await syncer.handle_reset("simulator.notice")
    assert await count(syncer.factory, SimTick) == 0 and state.last_synced_tick is None
    assert ("sim.reset", {"reason": "simulator.notice"}) in await bus_types(redis)


# --- slow sync / demand ------------------------------------------------------------------------------------
async def test_backfill_then_no_gaps_and_supply_arrivals(syncer, sim, state):
    sim.world["demand"] = {
        s: demand_rows(s, range(20), start_id=1 + i * 1000) for i, s in enumerate(STATION_IDS)
    }
    await syncer.tick_sync()
    assert await syncer.slow_sync() is True
    f = syncer.factory
    assert all(r["limit"] == "2000" for r in sim.demand_requests[:4])  # startup backfill uses the max limit
    assert (
        await count(f, DemandObservation) == 4 * 3 * 20
    )  # 12 rows per tick x 20 ticks... per 4 stations = 240
    assert state.demand_gaps == {} and state.needs_backfill is False and state.last_demand_tick == 19
    assert await count(f, SupplyArrivalRow) == 1
    # per tick: 4 stations x 3 fuels = 12 rows
    async with f() as s:
        per_tick = (
            await s.execute(
                select(func.count()).select_from(DemandObservation).where(DemandObservation.tick == 7)
            )
        ).scalar_one()
    assert per_tick == 12

    # incremental: new ticks arrive, limit is computed from ticks since last, re-inserting is harmless
    sim.demand_requests.clear()
    sim.world = make_world(25)
    sim.world["demand"] = {
        s: demand_rows(s, range(26), start_id=1 + i * 1000) for i, s in enumerate(STATION_IDS)
    }
    await syncer.tick_sync()
    await syncer.slow_sync()
    assert {r["limit"] for r in sim.demand_requests} == {str(incremental_limit(25 - 19))} == {"48"}
    assert await count(f, DemandObservation) == 4 * 3 * 26 and state.demand_gaps == {}


async def test_gap_detection(syncer, sim, state):
    rows = demand_rows("station-mirpur", range(10))
    rows = [r for r in rows if not (r["tick"] == 4 and r["fuel_type"] == "PETROL")]
    sim.world["demand"] = {"station-mirpur": rows}
    await syncer.tick_sync()
    await syncer.slow_sync()
    assert state.demand_gaps == {"station-mirpur/PETROL": 1}


async def test_stale_demand_history_is_not_persisted(syncer, sim):
    sim.world["demand"] = {"station-mirpur": demand_rows("station-mirpur", range(5))}
    await syncer.tick_sync()
    sim.headers = {"X-Simulator-Stale": "true"}
    await syncer.slow_sync()
    assert await count(syncer.factory, DemandObservation) == 0
    assert await count(syncer.factory, SupplyArrivalRow) == 0


async def test_slow_sync_failure_is_retried_next_time(syncer, sim, state):
    await syncer.tick_sync()
    sim.overrides["demand"] = httpx.Response(503, json={"error": {"code": "FAULT_INJECTED", "message": "x"}})
    assert await syncer.slow_sync() is False
    assert state.needs_backfill is True and state.last_slow_tick is None and syncer.slow_sync_due()
    del sim.overrides["demand"]
    assert await syncer.slow_sync() is True and state.needs_backfill is False


def test_incremental_limit():
    assert incremental_limit(0) == 30 and incremental_limit(8) == 54 and incremental_limit(10_000) == 2000


# --- retention ----------------------------------------------------------------------------------------------------
async def test_retention_only_trims_snapshots(syncer, sim):
    for tick in (10, 100):
        sim.world = make_world(tick)
        await syncer.tick_sync()
    async with syncer.factory() as s:
        s.add(Alert(kind="X", severity="LOW", message="keep me"))
        await s.commit()
    f = syncer.factory
    assert await count(f, SimTick) == 2
    # tick 3000 is a retention tick (multiple of 50): the sync itself trims ticks older than 3000 - 2000
    sim.world = make_world(3000)
    await syncer.tick_sync()
    assert await count(f, SimTick) == 1 and await count(f, StationSnapshot) == 12
    assert (
        await count(f, DepotSnapshot) == 6
        and await count(f, RouteSnapshot) == 2
        and await count(f, SimMetric) == 1
    )
    assert await count(f, Alert) == 1
    async with f() as s:  # explicit call is a no-op now, and never deletes below the cutoff
        assert await store.apply_retention(s, current_tick=3000, keep_ticks=2000) == 0
        assert await store.apply_retention(s, current_tick=100, keep_ticks=2000) == 0  # cutoff <= 0


# --- redis outage --------------------------------------------------------------------------------------------------
async def test_redis_outage_does_not_break_sync(syncer, sim):
    class Broken:
        async def publish(self, *a, **k):
            raise ConnectionError("redis down")

    syncer.bus = SafeBus(Broken())  # type: ignore[arg-type]
    before = counter("bus_publish_errors_total", {})
    assert await syncer.tick_sync() == 5
    assert counter("bus_publish_errors_total", {}) > before
