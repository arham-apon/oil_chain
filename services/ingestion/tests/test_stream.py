import asyncio
import time

import httpx
import pytest
import respx
from fsp_shared.models import SimAllocation
from fsp_shared.schemas import SSEEvent
from fsp_shared.sim_client import SimClient
from prometheus_client import REGISTRY
from sqlalchemy import func, select

from app.state import IngestionState, SafeBus
from app.stream_worker import EventProcessor, StreamWorker, next_backoff
from app.workers import SyncCoordinator, poll_fallback

from .conftest import bus_types
from .helpers import BASE, allocation, make_world


class FakeCoordinator:
    def __init__(self):
        self.ticks = 0
        self.fulls = 0

    def request_tick(self):
        self.ticks += 1

    def request_full(self):
        self.fulls += 1


def sse_body(*events):
    out = b": connected\n\n"
    for name, data in events:
        out += f"event: {name}\ndata: {data}\n\n".encode()
    return out


# --- StreamWorker ---------------------------------------------------------------------------------------------
async def test_worker_reconnects_after_fault_and_requests_full_sync(redis):
    from fsp_shared.bus import Bus

    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError  # stop the endless loop deterministically

    state, coord, queue = IngestionState(), FakeCoordinator(), asyncio.Queue(1000)
    fault = httpx.Response(503, json={"detail": {"code": "FAULT_INJECTED"}})
    ok = httpx.Response(
        200,
        content=sse_body(
            ("simulation.tick", '{"tick": 1, "sim_time": "2026-01-01T00:15:00"}'),
            ("simulation.tick", '{"tick": 2, "sim_time": "2026-01-01T00:30:00"}'),
        ),
    )
    before = REGISTRY.get_sample_value("sse_reconnects_total") or 0
    with respx.mock as router:
        router.get(f"{BASE}/v1/stream").mock(side_effect=[fault, ok, fault])
        client = SimClient(BASE)
        worker = StreamWorker(client, queue, state, coord, SafeBus(Bus(redis)), sleep=fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await worker.run()
        await client.aclose()
    assert queue.qsize() == 2
    assert (await queue.get()).data["tick"] == 1
    assert coord.fulls == 1  # only the successful connection triggers the resync
    assert state.sse_reconnects == 3 and state.sse_connected is False
    assert (REGISTRY.get_sample_value("sse_reconnects_total") or 0) == before + 3
    await asyncio.sleep(0)  # let the "fault cleared" publish task finish
    faults = [p for t, p in await bus_types(redis) if t == "sim.fault"]
    assert faults[0] == {"type": "stream_disconnect", "active": True}
    assert {"type": "stream_disconnect", "active": False} in faults
    assert all(0 < s <= 30 for s in sleeps)


async def test_worker_marks_connected_even_if_only_comments_arrive(redis):
    """A paused simulator only sends ': connected' / ': keepalive' — that must still count as connected."""
    from fsp_shared.bus import Bus

    state, coord = IngestionState(), FakeCoordinator()
    seen = {}

    async def fake_sleep(d):
        raise asyncio.CancelledError

    with respx.mock as router:
        router.get(f"{BASE}/v1/stream").mock(return_value=httpx.Response(200, content=b": connected\n\n"))
        client = SimClient(BASE)
        worker = StreamWorker(client, asyncio.Queue(10), state, coord, SafeBus(Bus(redis)), sleep=fake_sleep)
        orig = state.set_sse
        state.set_sse = lambda c: (seen.setdefault("connected", c) if c else None, orig(c))[1]  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await worker.run()
        await client.aclose()
    assert seen["connected"] is True and coord.fulls == 1


async def test_full_queue_drops_events_and_requests_resync(redis):
    from fsp_shared.bus import Bus

    state, coord = IngestionState(), FakeCoordinator()
    queue = asyncio.Queue(2)
    worker = StreamWorker(SimClient(BASE), queue, state, coord, SafeBus(Bus(redis)))
    before = REGISTRY.get_sample_value("sse_events_dropped_total") or 0
    for i in range(4):
        worker._enqueue(SSEEvent("simulation.tick", {"tick": i}))
    assert queue.qsize() == 2 and coord.fulls == 2
    assert (REGISTRY.get_sample_value("sse_events_dropped_total") or 0) == before + 2


def test_backoff_grows_to_cap():
    b, seq = 0.25, []
    for _ in range(10):
        b = next_backoff(b)
        seq.append(b)
    assert seq[0] == 0.5 and seq[1] == 1.0 and seq[-1] == 30.0 and seq == sorted(seq)


# --- EventProcessor ------------------------------------------------------------------------------------------------
@pytest.fixture
async def processor(syncer, redis, state):
    coord = FakeCoordinator()
    return EventProcessor(
        asyncio.Queue(10), syncer, coord, syncer.factory, SafeBus(syncer.bus.bus), state
    ), coord


async def test_tick_event_triggers_debounced_sync_and_publishes(processor, redis, state):
    proc, coord = processor
    await proc.handle(SSEEvent("simulation.tick", {"tick": 42, "sim_time": "2026-01-01T10:30:00"}))
    assert coord.ticks == 1 and state.latest_tick_seen == 42
    assert ("tick", {"tick": 42, "sim_time": "2026-01-01T10:30:00"}) in await bus_types(redis)


async def test_allocation_event_upserts_publishes_and_requests_reget(processor, syncer, redis):
    proc, coord = processor
    syncer.state.prev_allocations = {}  # caches primed, so changes publish
    await proc.handle(SSEEvent("allocation.status_changed", allocation(3, "PENDING")))
    await proc.handle(
        SSEEvent("allocation.status_changed", allocation(3, "PENDING"))
    )  # duplicate: no 2nd publish
    await proc.handle(SSEEvent("allocation.status_changed", allocation(3, "IN_TRANSIT", departure_tick=6)))
    async with syncer.factory() as s:
        row = (await s.execute(select(SimAllocation))).scalar_one()
    assert row.status == "IN_TRANSIT" and row.departure_tick == 6
    changed = [p for t, p in await bus_types(redis) if t == "allocation.changed"]
    assert [c["status"] for c in changed] == ["PENDING", "IN_TRANSIT"]
    assert coord.ticks == 3


async def test_invalid_allocation_event_is_rejected_and_falls_back_to_rest(processor, syncer):
    proc, coord = processor
    before = (
        REGISTRY.get_sample_value("sim_payload_invalid_total", {"endpoint": "sse:allocation.status_changed"})
        or 0
    )
    await proc.handle(SSEEvent("allocation.status_changed", {"id": "x", "status": "NOPE"}))
    assert (
        REGISTRY.get_sample_value("sim_payload_invalid_total", {"endpoint": "sse:allocation.status_changed"})
        or 0
    ) == before + 1
    assert coord.ticks == 1
    async with syncer.factory() as s:
        assert (await s.execute(select(func.count()).select_from(SimAllocation))).scalar_one() == 0


async def test_inventory_event_requests_sync_and_unknown_events_are_ignored(processor):
    proc, coord = processor
    await proc.handle(SSEEvent("inventory.updated", {"entity_type": "depot", "entity_id": "depot-gazipur"}))
    await proc.handle(SSEEvent("something.new", {}))
    assert coord.ticks == 1


async def test_reset_notice_wipes_and_requests_full_resync(processor, syncer, sim, redis):
    proc, coord = processor
    await syncer.tick_sync()
    await proc.handle(SSEEvent("simulator.notice", {"message": "Simulation reset"}))
    assert coord.fulls == 1 and syncer.state.last_synced_tick is None
    assert "sim.reset" in [t for t, _ in await bus_types(redis)]
    await proc.handle(SSEEvent("simulator.notice", {"level": "error", "message": "runner exception"}))
    assert coord.fulls == 1  # not a reset


async def test_processor_survives_a_failing_handler(processor):
    proc, _ = processor

    async def boom(event):
        raise RuntimeError("bad")

    proc.handle = boom  # type: ignore[method-assign]
    proc.queue.put_nowait(SSEEvent("simulation.tick", {}))
    task = asyncio.create_task(proc.run())
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()


# --- SyncCoordinator ---------------------------------------------------------------------------------------------------
class FakeSyncer:
    def __init__(self, fail_first=0):
        self.calls, self.fail_first = [], fail_first

    async def full_sync(self):
        self.calls.append("full")
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("simulator down")
        return 1

    async def tick_sync(self):
        self.calls.append("tick")
        return 1

    async def slow_sync(self):
        self.calls.append("slow")

    def slow_sync_due(self):
        return False

    def demand_behind(self):
        return self.behind

    behind = False


async def test_coordinator_first_run_is_full_and_debounces_bursts():
    syncer, state = FakeSyncer(), IngestionState()
    coord = SyncCoordinator(syncer, state, max_hz=20)  # 50 ms min interval
    task = asyncio.create_task(coord.run())
    await asyncio.sleep(0.15)
    assert syncer.calls[0] == "full"
    n = len(syncer.calls)
    for _ in range(200):  # a burst of 200 requests coalesces
        coord.request_tick()
    await asyncio.sleep(0.12)
    assert 1 <= len(syncer.calls) - n <= 3
    task.cancel()


async def test_coordinator_retries_failed_full_sync():
    syncer, state = FakeSyncer(fail_first=2), IngestionState()
    coord = SyncCoordinator(syncer, state, max_hz=100)
    task = asyncio.create_task(coord.run())
    await asyncio.sleep(1.6)
    assert syncer.calls[:3] == ["full", "full", "full"]  # kept trying the full sync until it worked
    assert task.done() is False
    task.cancel()


async def test_poll_fallback_only_after_sse_down_over_five_seconds():
    state, coord = IngestionState(), FakeCoordinator()
    with respx.mock as router:
        router.get(f"{BASE}/v1/instance").mock(
            return_value=httpx.Response(200, json=make_world(9)["instance"])
        )
        client = SimClient(BASE)
        task = asyncio.create_task(poll_fallback(client, state, coord))
        state.sse_connected = True
        await asyncio.sleep(0.7)
        assert coord.ticks == 0  # SSE healthy: no polling
        state.sse_connected = False
        state.sse_down_since = time.monotonic()  # just went down
        await asyncio.sleep(0.7)
        assert coord.ticks == 0  # < 5 s
        state.sse_down_since = time.monotonic() - 6
        await asyncio.sleep(0.7)
        assert coord.ticks >= 1 and state.latest_tick_seen == 9
        task.cancel()
        await client.aclose()


async def test_heartbeat_catches_demand_history_up_but_ticks_do_not(monkeypatch):
    from app import workers

    monkeypatch.setattr(workers, "HEARTBEAT_S", 0.2)
    syncer, state = FakeSyncer(), IngestionState()
    syncer.behind = True
    coord = SyncCoordinator(syncer, state, max_hz=100)
    task = asyncio.create_task(coord.run())
    await asyncio.sleep(0.1)  # initial full sync only
    assert "slow" not in syncer.calls
    coord.request_tick()  # a tick-triggered sync is not a heartbeat: demand catch-up waits (slow sync every 8 ticks)
    await asyncio.sleep(0.1)
    assert "slow" not in syncer.calls and "tick" in syncer.calls
    await asyncio.sleep(0.4)  # heartbeat fires while demand is behind
    assert "slow" in syncer.calls
    n = syncer.calls.count("slow")
    syncer.behind = False
    await asyncio.sleep(0.5)
    assert syncer.calls.count("slow") == n  # caught up: heartbeats stop pulling demand
    task.cancel()
