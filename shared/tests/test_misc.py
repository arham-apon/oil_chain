import io
import json
import logging

import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fsp_shared import world_constants as wc
from fsp_shared.bus import STREAM_KEY, Bus
from fsp_shared.config import Settings
from fsp_shared.logging import bind_context, clear_context, configure_logging, get_logger
from fsp_shared.metrics import install_metrics
from fsp_shared.timeutil import hour_of_day, parse_sim_time, project_sim_time, ticks_per_hour


# --- timeutil -----------------------------------------------------------------------------------
def test_hour_of_day_parses_string_not_tick_math():
    assert hour_of_day("2026-01-01T07:45:00") == 7
    assert hour_of_day("2026-01-01T23:00:00+00:00") == 23
    assert hour_of_day("2026-01-01T23:59:59Z") == 23


def test_parse_naive_is_utc_and_offsets_are_normalised():
    assert parse_sim_time("2026-01-01T00:00:00").utcoffset().total_seconds() == 0
    assert parse_sim_time("2026-01-01T06:00:00+06:00").hour == 0


def test_ticks_per_hour_uses_tick_minutes():
    assert ticks_per_hour(15) == 4
    assert ticks_per_hour(30) == 2
    assert ticks_per_hour(60) == 1
    with pytest.raises(ValueError):
        ticks_per_hour(0)


def test_project_sim_time():
    t = project_sim_time("2026-01-01T00:00:00", 0, 10, 15)
    assert (t.hour, t.minute) == (2, 30)


# --- world constants -------------------------------------------------------------------------------
def test_world_constants_match_guide():
    assert len(wc.DEPOTS) == 2 and len(wc.STATIONS) == 4 and len(wc.ROUTES) == 6
    assert wc.STATIONS["station-tongi"]["initial_inventory"]["DIESEL"] == 11_000  # C1
    assert wc.ROUTES["route-gazipur-tongi"]["max_shipment"] == 6_500
    for r in wc.ROUTES.values():
        assert wc.STATIONS[r["station"]]["region_id"] is not None and r["depot"] in wc.DEPOTS


def test_single_route_stations_c2():
    assert wc.SINGLE_ROUTE_STATIONS == {"station-tongi", "station-coxsbazar"}
    assert set(wc.routes_to_station("station-mirpur")) == {"route-gazipur-mirpur", "route-patiya-mirpur"}
    assert set(wc.routes_to_station("station-karnaphuli")) == {
        "route-patiya-karnaphuli",
        "route-gazipur-karnaphuli",
    }


def test_hour_factors_and_baseline():
    assert wc.hour_factor("industrial", 6) == 1.55 and wc.hour_factor("industrial", 17) == 1.55
    assert wc.hour_factor("industrial", 18) == 0.45 and wc.hour_factor("industrial", 5) == 0.45
    assert wc.hour_factor("regional", 20) == 1.25 and wc.hour_factor("regional", 21) == 0.65
    assert wc.hour_factor("urban_high", 8) == 1.45 and wc.hour_factor("urban_high", 12) == 0.70
    assert wc.hour_factor("highway", 17) == 1.35 and wc.hour_factor("highway", 3) == 0.75
    # industrial diesel, noon, Dhaka, 15-min tick: 14000 * 15/1440 * 1.55 * 1.0
    assert wc.baseline_demand("industrial", "DIESEL", 12, 15) == pytest.approx(14_000 * 15 / 1440 * 1.55)
    assert wc.baseline_demand(
        "highway", "OCTANE", 7, 15, region_factor=1.08, demand_multiplier=1.8
    ) == pytest.approx(6_200 * 15 / 1440 * 1.35 * 1.08 * 1.8)


# --- config ------------------------------------------------------------------------------------------
def test_secrets_have_no_defaults(monkeypatch):
    for k in ("DATABASE_URL", "TYPESAFE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(Exception) as exc:
        Settings(_env_file=None)
    msg = str(exc.value)
    assert "DATABASE_URL" in msg and "TYPESAFE_API_KEY" in msg and "GEMINI_API_KEY" in msg


def test_settings_defaults_match_env_example(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    s = Settings(_env_file=None)
    assert s.TYPESAFE_API_KEY == "" and s.GEMINI_API_KEY == ""  # empty = fallback mode is allowed
    assert (s.DECISION_INTERVAL_TICKS, s.AUTO_APPROVE_NOUL_MIN, s.AUTO_APPROVE_URGENCY_MAX) == (4, 0.85, 4.5)
    assert (s.STAGED_DECISION_TTL_TICKS, s.MIN_LOT_LITERS, s.DEPOT_CONSTRAINT_DERATE) == (16, 500, 0.5)
    assert (s.JEV_TIMEOUT_MS, s.GEMINI_TIMEOUT_MS, s.BREAKER_FAILURE_THRESHOLD, s.BREAKER_OPEN_SECONDS) == (
        600,
        3000,
        5,
        30,
    )
    assert (
        s.ROUTE_CAP_MODE == "per_route_per_cycle"
        and s.SIM_HTTP_TIMEOUT_S == 3.0
        and s.GEMINI_MODEL == "gemini-2.0-flash"
    )


def test_env_example_parses_into_settings(monkeypatch, tmp_path):
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / ".env.example"
    for k in ("DATABASE_URL", "TYPESAFE_API_KEY", "GEMINI_API_KEY", "ROUTE_CAP_MODE"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=example)
    assert s.ROUTE_CAP_MODE == "per_route_per_cycle" and s.HORIZON_TICKS == 24 and s.DEMO_CONTROLS is True


# --- logging -------------------------------------------------------------------------------------------
def test_json_logs_have_standard_fields():
    buf = io.StringIO()
    configure_logging("unit-test", "INFO", stream=buf)
    clear_context()
    bind_context(tick=42, decision_id="d-1", idempotency_key="fsp-d-1", trace_id="t-1")
    get_logger().info("allocation_posted", quantity=3000)
    logging.getLogger("stdlib.check").warning("via stdlib")
    clear_context()
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    rec = lines[0]
    assert rec["service"] == "unit-test" and rec["level"] == "info" and rec["event"] == "allocation_posted"
    assert (rec["tick"], rec["decision_id"], rec["idempotency_key"], rec["trace_id"]) == (
        42,
        "d-1",
        "fsp-d-1",
        "t-1",
    )
    assert rec["quantity"] == 3000 and "timestamp" in rec
    assert lines[1]["event"] == "via stdlib" and lines[1]["service"] == "unit-test"


# --- metrics middleware ------------------------------------------------------------------------------------
async def test_metrics_middleware_and_endpoint():
    app = FastAPI()
    install_metrics(app, "unit")

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/items/1")).status_code == 200
        assert (await c.get("/items/2")).status_code == 200
        assert (await c.get("/items/x")).status_code == 422
        assert (await c.get("/nope")).status_code == 404
        body = (await c.get("/metrics")).text
    assert (
        'http_requests_total{method="GET",route="/items/{item_id}",service="unit",status="200"} 2.0' in body
    )
    assert 'status="422"' in body and 'route="unmatched"' in body
    assert 'http_request_duration_seconds_bucket{le="0.1",route="/items/{item_id}",service="unit"}' in body


# --- bus (fakeredis) -------------------------------------------------------------------------------------------
async def test_bus_publish_consume_and_maxlen():
    redis = FakeAsyncRedis(decode_responses=True)
    bus = Bus(redis)
    await bus.ensure_group("decision", start="0")
    await bus.ensure_group("decision", start="0")  # idempotent (BUSYGROUP swallowed)
    await bus.ensure_group("gateway", start="0")
    mid = await bus.publish("state.updated", {"tick": 5, "stale": False})
    await bus.publish("sim.fault", {"type": "stale_data"})

    msgs = await bus.read("decision", "d1", block_ms=10)
    assert [m.type for m in msgs] == ["state.updated", "sim.fault"]
    assert msgs[0].payload == {"tick": 5, "stale": False} and msgs[0].id == mid and msgs[0].ts > 0
    await bus.ack("decision", *[m.id for m in msgs])
    assert await bus.read("decision", "d1", block_ms=10) == []  # acked, nothing pending
    # a second consumer group receives its own copy
    assert len(await bus.read("gateway", "g1", block_ms=10)) == 2
    assert await redis.xlen(STREAM_KEY) == 2
    await bus.close()


async def test_bus_stream_is_trimmed_approximately():
    redis = FakeAsyncRedis(decode_responses=True)
    bus = Bus(redis)
    for i in range(50):
        await bus.publish("tick", {"tick": i})
    assert await redis.xlen(STREAM_KEY) <= 50  # approximate trim never exceeds what we wrote
    info = await redis.xinfo_stream(STREAM_KEY)
    assert info["length"] == 50
