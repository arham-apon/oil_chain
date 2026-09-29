"""Phase 3 acceptance: the ingestion runtime against the REAL simulator image, Postgres and Redis.

Run with the stack up, e.g.:
    SIM_LIVE_URL=http://localhost:8000 \
    TEST_DATABASE_URL=postgresql+asyncpg://test:testpw@127.0.0.1:15432/fsp_test \
    TEST_REDIS_URL=redis://127.0.0.1:16379/0 pytest tests/integration/test_ingestion_pipeline.py

WARNING: the database schema is wiped and the simulator is reset (admin endpoints) for every test.
Faults are injected only through /admin/faults; the ingestion code never calls /admin/*.
"""

import asyncio
import json
import os
import time

import pytest
import redis.asyncio as aioredis
from app.main import build_runtime
from fsp_shared.bus import STREAM_KEY, Bus
from fsp_shared.config import Settings
from fsp_shared.sim_client import SimClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SIM = os.environ.get("SIM_LIVE_URL")
DB = os.environ.get("TEST_DATABASE_URL")
REDIS = os.environ.get("TEST_REDIS_URL")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (SIM and DB and REDIS),
        reason="SIM_LIVE_URL / TEST_DATABASE_URL / TEST_REDIS_URL not set",
    ),
]


async def wait_for(predicate, timeout, interval=0.25, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {message}")


class Harness:
    def __init__(self, runtime, admin, engine, redis_client):
        self.rt, self.admin, self.engine, self.redis = (
            runtime,
            admin,
            engine,
            redis_client,
        )
        self.state = runtime.state

    async def scalar(self, sql, **params):
        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar_one()

    async def max_tick(self):
        return await self.scalar("SELECT coalesce(max(tick), -1) FROM sim_ticks")

    async def bus(self):
        return [
            (f["type"], json.loads(f["payload"]))
            for _, f in await self.redis.xrange(STREAM_KEY)
        ]

    def tasks_alive(self):
        return all(not t.done() for t in self.rt.tasks)


async def _reset_world(admin):
    await admin.admin_clear_faults()
    await admin.admin_pause()
    await admin.admin_reset()
    await admin.admin_pause()


@pytest.fixture
async def harness(monkeypatch):
    """Factory: ``await harness(before_start=...)`` -> started Harness. Cleans everything up afterwards."""
    monkeypatch.setenv("DATABASE_URL", DB)
    monkeypatch.setenv("REDIS_URL", REDIS)
    monkeypatch.setenv("SIMULATOR_URL", SIM)
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    settings = Settings(_env_file=None)

    admin = SimClient(SIM, 5.0)
    engine = create_async_engine(DB)
    redis_client = aioredis.from_url(REDIS, decode_responses=True)
    started: list[Harness] = []

    async def make(before_start=None):
        await _reset_world(admin)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await redis_client.delete(STREAM_KEY)
        if before_start:
            await before_start(admin)
        runtime = build_runtime(
            settings, bus=Bus(aioredis.from_url(REDIS, decode_responses=True))
        )
        await runtime.start()
        h = Harness(runtime, admin, engine, redis_client)
        started.append(h)
        return h

    yield make
    for h in started:
        await h.rt.stop()
    await _reset_world(admin)
    await admin.aclose()
    await redis_client.aclose()
    await engine.dispose()


# 1 --------------------------------------------------------------------------------------------------------------
async def test_running_sim_advances_sim_ticks_with_full_snapshots(harness):
    h = await harness()
    await wait_for(
        lambda: h.state.sse_connected and h.state.last_synced_tick is not None,
        15,
        message="SSE + first sync",
    )
    await h.admin.admin_run()
    samples = []
    for _ in range(8):
        await asyncio.sleep(1.0)
        samples.append(await h.max_tick())
    assert samples == sorted(samples) and samples[-1] - samples[0] >= 30, (
        samples
    )  # ~8 ticks/s while RUNNING
    assert h.tasks_alive() and h.state.sse_connected

    await (
        h.admin.admin_pause()
    )  # freeze the world so the DB checks below are not racing a running sync
    final = (await h.admin.instance()).data.tick
    await wait_for(
        lambda: h.state.last_synced_tick == final, 10, message="final tick synced"
    )
    await asyncio.sleep(0.5)
    n_ticks = await h.scalar("SELECT count(*) FROM sim_ticks")
    assert n_ticks >= 15
    # every synced tick has a complete snapshot set
    assert (
        await h.scalar(
            "SELECT count(*) FROM (SELECT tick FROM station_snapshots GROUP BY tick HAVING count(*) <> 12) x"
        )
        == 0
    )
    assert (
        await h.scalar(
            "SELECT count(*) FROM (SELECT tick FROM depot_snapshots GROUP BY tick HAVING count(*) <> 6) x"
        )
        == 0
    )
    assert (
        await h.scalar(
            "SELECT count(*) FROM (SELECT tick FROM route_snapshots GROUP BY tick HAVING count(*) <> 6) x"
        )
        == 0
    )
    assert (
        await h.scalar("SELECT count(DISTINCT tick) FROM station_snapshots") == n_ticks
    )
    assert await h.scalar("SELECT count(*) FROM sim_metrics") >= 1
    assert await h.scalar("SELECT count(*) FROM supply_arrivals") == 22
    assert await h.scalar("SELECT count(*) FROM sim_ticks WHERE stale") == 0

    types = {t for t, _ in await h.bus()}
    assert {"state.updated", "tick"} <= types
    status = h.state.to_status()
    assert (
        status["sse"]["connected"]
        and status["sync_lag_ticks"] is not None
        and status["sync_lag_ticks"] < 12
    )
    assert status["last_sync_latency_ms"] < 500


# 2 --------------------------------------------------------------------------------------------------------------
async def test_demand_history_has_12_rows_per_tick_and_no_gaps(harness):
    h = await harness()
    await wait_for(lambda: h.state.sse_connected, 15, message="SSE")
    await h.admin.admin_run()
    await asyncio.sleep(12)
    await h.admin.admin_pause()
    final = (await h.admin.instance()).data.tick
    await wait_for(
        lambda: h.state.last_synced_tick == final, 10, message="final tick synced"
    )
    await h.rt.syncer.slow_sync(
        backfill=False
    )  # the periodic slow sync, forced so the assertion is deterministic

    async with h.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT tick, count(*) FROM demand_observations GROUP BY tick ORDER BY tick"
                )
            )
        ).all()
    assert rows and all(n == 12 for _, n in rows), [r for r in rows if r[1] != 12]
    ticks = [t for t, _ in rows]
    assert ticks == list(range(ticks[0], ticks[-1] + 1)), (
        "gap in demand_observations ticks"
    )
    assert ticks[0] <= 1 and ticks[-1] >= final - 1, (ticks[0], ticks[-1], final)
    assert h.state.demand_gaps == {} and h.tasks_alive()

    # startup backfill: restart against an already-advanced simulator and get the whole history back
    await h.rt.stop()
    h2 = await harness_restart(h)
    await wait_for(lambda: h2.state.needs_backfill is False, 15, message="backfill")
    n = await h2.scalar("SELECT count(*) FROM demand_observations")
    assert n == 12 * (final + 1) or n == 12 * final, (n, final)
    assert h2.state.demand_gaps == {}


async def harness_restart(old: Harness) -> Harness:
    """New runtime on the SAME database/simulator (simulates an ingestion-svc restart)."""
    rt = build_runtime(
        old.rt.settings, bus=Bus(aioredis.from_url(REDIS, decode_responses=True))
    )
    await rt.start()
    h = Harness(rt, old.admin, old.engine, old.redis)
    old.rt = rt  # so the fixture teardown stops the new one too
    return h


# 3 --------------------------------------------------------------------------------------------------------------
async def test_stream_disconnect_polling_fallback_then_reconnect_and_full_sync(harness):
    async def inject(admin):
        await admin.admin_create_fault("stream_disconnect", 20)

    h = await harness(
        before_start=inject
    )  # SSE cannot connect: 503 {"detail": {"code": "FAULT_INJECTED"}}
    await h.admin.admin_run()
    await asyncio.sleep(3)
    assert h.state.sse_connected is False and h.state.stream_fault is True
    t_before_fallback = await h.max_tick()

    # after 5 s of SSE downtime the platform polls /v1/instance and keeps sim_ticks moving
    await wait_for(lambda: h.state.sse_down_for() > 5, 6, message="5 s of SSE downtime")
    await asyncio.sleep(4)
    t_during = await h.max_tick()
    assert t_during - t_before_fallback >= 20, (t_before_fallback, t_during)
    assert not h.state.sse_connected and h.tasks_alive()
    assert h.state.sse_reconnects >= 1

    # the fault auto-expires; the worker reconnects (backoff <= 30 s) and requests a full REST sync
    syncs_before = h.state.last_sync_at
    await wait_for(
        lambda: h.state.sse_connected,
        45,
        message="SSE reconnect after the fault expired",
    )
    await wait_for(
        lambda: h.state.last_sync_at != syncs_before,
        5,
        message="full sync after reconnect",
    )
    assert h.state.stream_fault is False
    await asyncio.sleep(2)
    faults = [
        p
        for t, p in await h.bus()
        if t == "sim.fault" and p.get("type") == "stream_disconnect"
    ]
    assert faults[0]["active"] is True and faults[-1]["active"] is False
    assert (await h.max_tick()) > t_during


# 4 --------------------------------------------------------------------------------------------------------------
async def test_stale_data_flags_rows_alerts_and_publishes(harness):
    h = await harness()
    await wait_for(
        lambda: h.state.sse_connected and h.state.last_synced_tick is not None,
        15,
        message="SSE + first sync",
    )
    await h.admin.admin_run()
    await asyncio.sleep(2)
    await h.admin.admin_create_fault("stale_data", 8)
    await wait_for(lambda: h.state.stale, 5, message="stale flag")
    await asyncio.sleep(2)

    assert await h.scalar("SELECT count(*) FROM station_snapshots WHERE stale") >= 12
    assert await h.scalar("SELECT count(*) FROM depot_snapshots WHERE stale") >= 6
    assert await h.scalar("SELECT count(*) FROM alerts WHERE kind = 'STALE_DATA'") == 1
    faults = [
        p
        for t, p in await h.bus()
        if t == "sim.fault" and p.get("type") == "stale_data"
    ]
    assert faults and faults[0]["active"] is True
    assert (
        h.state.last_good and h.state.last_good["tick"] < await h.max_tick()
    )  # last non-stale cache retained

    # the first clean response clears the flag (re-fetch continues every second while stale)
    await wait_for(
        lambda: not h.state.stale, 15, message="stale cleared after fault expiry"
    )
    await asyncio.sleep(1)
    faults = [
        p
        for t, p in await h.bus()
        if t == "sim.fault" and p.get("type") == "stale_data"
    ]
    assert faults[-1]["active"] is False and h.tasks_alive()


# 5 --------------------------------------------------------------------------------------------------------------
async def test_error_rate_fault_ingestion_keeps_up_without_crashing(harness):
    h = await harness()
    await wait_for(
        lambda: h.state.sse_connected and h.state.last_synced_tick is not None,
        15,
        message="SSE + first sync",
    )
    await h.admin.admin_run()
    await asyncio.sleep(2)
    start = await h.max_tick()
    await h.admin.admin_create_fault("error_rate", 12, {"rate": 0.5})
    await asyncio.sleep(10)
    during = await h.max_tick()
    assert during - start >= 30, (
        start,
        during,
    )  # retries let syncs through despite 50% 503s
    assert h.tasks_alive()
    sim_state_seen = await h.rt.client.probe_state()
    await asyncio.sleep(4)  # fault expired
    await wait_for(
        lambda: h.state.last_sync_ok, 10, message="healthy syncs after the fault"
    )
    assert await h.max_tick() > during
    assert sim_state_seen is not None and h.tasks_alive()
    assert await h.scalar("SELECT count(*) FROM sim_ticks WHERE stale") == 0
