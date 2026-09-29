from datetime import timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from fsp_shared.demand import EventSpec

from app import features as F
from app.forecaster import Forecaster
from app.main import create_app
from app.model import ModelBundle, fit_pair
from app.state import InFlight, PairState, StateCache, WorldState
from app.trainer import ModelHolder

from .helpers import ALL_PAIRS, T0, sim_time, synth_series

TICK = 500
INV = {"DIESEL": 6000.0, "PETROL": 5000.0, "OCTANE": 3000.0}


def make_world(events=(), in_flight=(), n_rows=480, stale=False, inventory=INV, tick=TICK):
    pairs = {}
    for s, f in ALL_PAIRS:
        series = synth_series(s, f, tick, events=events)
        pairs[(s, f)] = PairState(
            s, f, None if inventory is None else inventory[f], 15000.0, "OPEN", stale,
            series["demand_liters"].to_numpy()[-96:], series["tick"].to_numpy()[-96:], n_rows,
        )
    return WorldState(
        tick=tick, sim_time=sim_time(tick), tick_minutes=15, sim_status="RUNNING", stale=stale, pairs=pairs,
        events=list(events), in_flight=list(in_flight), version=1,
    )


def make_bundle(events=(), pairs=ALL_PAIRS):
    models = {}
    for s, f in pairs:
        series = synth_series(s, f, TICK, events=events)
        models[(s, f)] = fit_pair(s, f, F.build_training_frame(s, f, series, 15, events))
    return ModelBundle("ridge-test", TICK, 15, models, n_rows=1)


@pytest.fixture(scope="module")
def bundle():
    return make_bundle()


class FakeCache(StateCache):
    def __init__(self, world):
        self.world = world


def forecaster(world, bundle=None):
    holder = ModelHolder()
    holder.bundle = bundle
    return Forecaster(FakeCache(world), holder, 24), holder


def parse(body):
    import json

    return json.loads(body)


def test_model_forecast_for_all_pairs_with_risk(bundle):
    fc, _ = forecaster(make_world(), bundle)
    body, _ = fc.forecast_json()
    r = parse(body)
    assert len(r["forecasts"]) == 12 and r["model_version"] == "ridge-test" and r["tick"] == TICK
    assert r["sim_time"].startswith("2026-01-06")
    f = next(x for x in r["forecasts"] if x["station_id"] == "station-tongi" and x["fuel_type"] == "DIESEL")
    assert f["source"] == "MODEL" and f["model_version"] == "ridge-test" and len(f["horizon"]) == 24
    assert f["horizon"][0]["tick"] == TICK + 1 and f["current_inventory"] == 6000.0
    assert f["burn_rate_lph"] == pytest.approx(f["horizon"][0]["mean"] * 4)
    assert 0.0 <= f["stockout_risk"] <= 1.0 and f["residual_sigma"] > 0 and f["data_stale"] is False


def test_risk_reflects_inventory_and_in_flight_arrivals(bundle):
    low = {"DIESEL": 800.0, "PETROL": 800.0, "OCTANE": 800.0}
    fc, _ = forecaster(make_world(inventory=low), bundle)
    base = {(x["station_id"], x["fuel_type"]): x for x in parse(fc.forecast_json(["station-tongi"], ["DIESEL"])[0])["forecasts"]}
    f0 = base[("station-tongi", "DIESEL")]
    assert f0["t_empty_ticks"] is not None and f0["t_empty_hours"] == pytest.approx(f0["t_empty_ticks"] / 4)
    assert f0["stockout_risk"] > 0.9

    flight = [InFlight("station-tongi", "DIESEL", TICK + 2, 9000.0),
              InFlight("station-tongi", "DIESEL", TICK - 1, 50000.0),  # already arrived: must be ignored
              InFlight("station-tongi", "PETROL", TICK + 2, 1.0)]
    fc2, _ = forecaster(make_world(inventory=low, in_flight=flight), bundle)
    f1 = parse(fc2.forecast_json(["station-tongi"], ["DIESEL"])[0])["forecasts"][0]
    assert f1["inflight_liters"] == 9000.0  # the stale (already arrived) allocation is not double counted
    assert f1["stockout_risk"] < f0["stockout_risk"] and (f1["t_empty_ticks"] is None or f1["t_empty_ticks"] > f0["t_empty_ticks"])


def test_scheduled_spike_in_events_raises_forecast_and_risk(bundle):
    plain, _ = forecaster(make_world(), bundle)
    spike = [EventSpec("demand_spike", TICK + 2, TICK + 20, {"station_ids": ["station-tongi"], "multiplier": 1.8}, "SCHEDULED")]
    spiked, _ = forecaster(make_world(events=spike), bundle)
    a = parse(plain.forecast_json(["station-tongi"], ["DIESEL"])[0])["forecasts"][0]
    b = parse(spiked.forecast_json(["station-tongi"], ["DIESEL"])[0])["forecasts"][0]
    assert b["horizon"][5]["mean"] > 1.5 * a["horizon"][5]["mean"]
    assert b["stockout_risk"] >= a["stockout_risk"]


def test_baseline_fallback_when_no_model_or_cold_start(bundle):
    for fc in (forecaster(make_world(), None)[0], forecaster(make_world(n_rows=50), bundle)[0]):
        f = parse(fc.forecast_json(["station-mirpur"], ["PETROL"])[0])["forecasts"][0]
        assert f["source"] == "BASELINE_FALLBACK" and f["model_version"] is None and len(f["horizon"]) == 24
        assert f["horizon"][0]["sigma"] == pytest.approx(0.10 * f["horizon"][0]["mean"])  # sigma = noise x baseline


def test_demand_history_trailing_the_snapshot_is_aligned(bundle):
    """Demand rows can lag the snapshot tick by several ticks: the horizon must still start at world.tick + 1."""
    full = make_world()
    lagged = make_world()
    for ps in lagged.pairs.values():
        ps.demand_tail, ps.tail_ticks = ps.demand_tail[:-6], ps.tail_ticks[:-6]  # newest 6 ticks not synced yet
    ahead = make_world()
    for ps in ahead.pairs.values():  # demand rows newer than the snapshot tick must be ignored, not extrapolated from
        ps.tail_ticks = ps.tail_ticks + 3
    a = parse(forecaster(full, bundle)[0].forecast_json(["station-tongi"], ["DIESEL"], 12)[0])["forecasts"][0]
    b = parse(forecaster(lagged, bundle)[0].forecast_json(["station-tongi"], ["DIESEL"], 12)[0])["forecasts"][0]
    c = parse(forecaster(ahead, bundle)[0].forecast_json(["station-tongi"], ["DIESEL"], 12)[0])["forecasts"][0]
    for f in (a, b, c):
        assert [p["tick"] for p in f["horizon"]] == list(range(TICK + 1, TICK + 13))
    for pa, pb in zip(a["horizon"], b["horizon"], strict=True):
        assert pb["mean"] == pytest.approx(pa["mean"], rel=0.12)  # lag features are a weak signal: same shape/scale


def test_partial_bundle_mixes_sources():
    part = make_bundle(pairs=[("station-mirpur", "DIESEL")])
    fc, _ = forecaster(make_world(), part)
    sources = {(x["station_id"], x["fuel_type"]): x["source"] for x in parse(fc.forecast_json()[0])["forecasts"]}
    assert sources[("station-mirpur", "DIESEL")] == "MODEL" and sources[("station-tongi", "DIESEL")] == "BASELINE_FALLBACK"


def test_unknown_inventory_still_forecasts(bundle):
    fc, _ = forecaster(make_world(inventory=None), bundle)
    f = parse(fc.forecast_json(["station-mirpur"], ["DIESEL"])[0])["forecasts"][0]
    assert f["current_inventory"] is None and f["t_empty_ticks"] is None and f["burn_rate_lph"] > 0


def test_selection_horizon_and_memoisation(bundle):
    fc, holder = forecaster(make_world(), bundle)
    body1, _ = fc.forecast_json(["station-tongi"], None, 8)
    r = parse(body1)
    assert len(r["forecasts"]) == 3 and all(len(x["horizon"]) == 8 for x in r["forecasts"])
    assert fc.forecast_json(["station-tongi"], None, 8)[0] is body1  # memo hit returns the same bytes
    assert fc.forecast_json(["station-tongi"], None, 200)[0] != body1  # horizon is clamped to <= 96
    assert len(parse(fc.forecast_json(None, None, 500)[0])["forecasts"][0]["horizon"]) == 96
    holder.set(make_bundle(pairs=[("station-tongi", "DIESEL")]))  # model change invalidates the memo
    assert fc.forecast_json(["station-tongi"], None, 8)[0] is not body1
    fc.cache.world = make_world(tick=TICK + 1)
    fc.cache.world.version = 2
    assert parse(fc.forecast_json(["station-tongi"], None, 8)[0])["tick"] == TICK + 1


# --- HTTP API ------------------------------------------------------------------------------------------------------
class FakeRuntime:
    def __init__(self, world, bundle):
        self.forecaster, self.holder = forecaster(world, bundle)
        self.cache = self.forecaster.cache
        self.engine = None
        self.trainer = self

    async def start(self):
        pass

    async def stop(self):
        pass

    async def list_models(self):
        return [{"version": "ridge-test", "active": True}]

    async def train(self):
        from app.trainer import TrainingError

        raise TrainingError("no simulator state has been ingested yet")


@pytest.fixture
def client(monkeypatch, bundle):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/z")
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    from fsp_shared.config import get_settings

    get_settings.cache_clear()
    app = create_app(lambda settings: FakeRuntime(make_world(), bundle))
    with TestClient(app) as c:
        yield c


def test_api_forecast_and_validation(client):
    r = client.post("/forecast", json={"station_ids": ["station-tongi"], "fuel_types": ["DIESEL"], "horizon": 12})
    assert r.status_code == 200 and "x-state-age" in r.headers
    assert len(r.json()["forecasts"][0]["horizon"]) == 12
    assert len(client.post("/forecast").json()["forecasts"]) == 12  # empty body -> all pairs, default horizon
    assert client.post("/forecast", json={"station_ids": ["station-nope"]}).status_code == 422
    assert client.post("/forecast", json={"fuel_types": ["KEROSENE"]}).status_code == 422
    assert client.post("/forecast", json={"horizon": 0}).status_code == 422
    assert client.post("/forecast", json={"horizon": 97}).status_code == 422


def test_api_get_forecast_matches_post(client):
    g = client.get("/forecast", params={"station_id": ["station-tongi", "station-mirpur"], "fuel_type": "DIESEL", "horizon": 6})
    p = client.post("/forecast", json={"station_ids": ["station-tongi", "station-mirpur"], "fuel_types": ["DIESEL"], "horizon": 6})
    assert g.status_code == 200 and g.json() == p.json() and len(g.json()["forecasts"]) == 2
    assert client.get("/forecast").status_code == 200 and len(client.get("/forecast").json()["forecasts"]) == 12
    assert client.get("/forecast", params={"station_id": "nope"}).status_code == 422
    assert client.get("/forecast", params={"horizon": 200}).status_code == 422


def test_api_models_train_metrics(client):
    assert client.post("/train").status_code == 409
    m = client.get("/models").json()
    assert m["active"]["version"] == "ridge-test" and len(m["active"]["pairs"]) == 12 and m["registry"][0]["active"]
    body = client.get("/metrics").text
    assert "forecast_source_total" in body and "forecast_inference_seconds_bucket" in body and "forecast_mae" in body


def test_api_no_state_returns_503(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/z")
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    from fsp_shared.config import get_settings

    get_settings.cache_clear()
    app = create_app(lambda s: FakeRuntime(None, None))
    with TestClient(app) as c:
        assert c.post("/forecast").status_code == 503


def test_forecast_latency_budget_for_all_pairs(bundle):
    """Spec acceptance: /forecast for all 12 pairs, p95 < 30 ms (computed path, i.e. a memo MISS every time)."""
    import time

    fc, _ = forecaster(make_world(), bundle)
    fc.forecast_json()  # warm up
    times = []
    for i in range(60):
        fc.cache.world.version += 1  # new snapshot each time -> no memo hit
        t0 = time.perf_counter()
        fc.forecast_json()
        times.append((time.perf_counter() - t0) * 1000)
    p95 = float(np.percentile(times, 95))
    assert p95 < 30.0, f"p95 {p95:.1f} ms (median {np.median(times):.1f} ms)"
    assert T0.tzinfo is timezone.utc
