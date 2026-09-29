"""Trainer + state cache against a real Postgres (TEST_DATABASE_URL); skipped otherwise."""

import json
import os

import pytest
from fsp_shared import migrate
from fsp_shared import world_constants as wc
from fsp_shared.demand import EventSpec
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.forecaster import Forecaster
from app.state import StateCache
from app.trainer import ModelHolder, Trainer, TrainingError

from .helpers import ALL_PAIRS, sim_time, synth_series

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

TICK = 400


@pytest.fixture
async def engine():
    e = create_async_engine(URL)
    async with e.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await migrate.upgrade_head(URL)
    yield e
    await e.dispose()


async def seed(engine, n_ticks=TICK, events=(), stale=False, outage_ticks=()):
    """Write what ingestion-svc would have persisted after ``n_ticks`` ticks."""
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO sim_ticks (tick, sim_time, status, tick_minutes, stale) VALUES (:t, :st, 'RUNNING', 15, :stale)"),
            {"t": n_ticks, "st": sim_time(n_ticks), "stale": stale},
        )
        next_id = 1
        for s, f in ALL_PAIRS:
            series = synth_series(s, f, n_ticks, events=events)
            rows = []
            for tick, st, d in series.itertuples(index=False):
                rows.append({"id": next_id, "s": s, "f": f, "t": int(tick), "st": st, "d": float(d)})
                next_id += 1
            await conn.execute(
                text("INSERT INTO demand_observations (id, station_id, fuel_type, tick, sim_time, demand_liters, served_liters, unmet_liters) "
                     "VALUES (:id, :s, :f, :t, :st, :d, :d, 0)"),
                rows,
            )
            status = "OUTAGE" if False else "OPEN"
            await conn.execute(
                text("INSERT INTO station_snapshots (tick, station_id, fuel_type, inventory, capacity, demand_multiplier, status, stale) "
                     "VALUES (:t, :s, :f, :inv, :cap, 1.0, :status, :stale)"),
                {"t": n_ticks, "s": s, "f": f, "inv": 5000.0, "cap": wc.STATIONS[s]["capacity"][f], "status": status, "stale": stale},
            )
            # an older snapshot that must NOT win (latest tick per pair is used)
            await conn.execute(
                text("INSERT INTO station_snapshots (tick, station_id, fuel_type, inventory, capacity, demand_multiplier, status) "
                     "VALUES (:t, :s, :f, 1, 1, 1.0, 'OPEN')"),
                {"t": n_ticks - 5, "s": s, "f": f},
            )
        for rid, r in wc.ROUTES.items():
            await conn.execute(
                text("INSERT INTO route_snapshots (tick, route_id, status, transit_ticks, max_shipment) VALUES (:t, :r, 'AVAILABLE', :tt, :m)"),
                {"t": n_ticks, "r": rid, "tt": r["transit_ticks"], "m": r["max_shipment"]},
            )
        for i, e in enumerate(events, start=1):
            await conn.execute(
                text("INSERT INTO sim_events (id, type, start_tick, end_tick, status, parameters, first_seen_tick) "
                     "VALUES (:i, :ty, :s, :e, :st, CAST(:p AS jsonb), 0)"),
                {"i": i, "ty": e.type, "s": e.start_tick, "e": e.end_tick, "st": e.status, "p": json.dumps(dict(e.parameters))},
            )
        for t in outage_ticks:
            await conn.execute(
                text("INSERT INTO station_snapshots (tick, station_id, fuel_type, inventory, capacity, demand_multiplier, status) "
                     "VALUES (:t, 'station-tongi', 'DIESEL', 0, 1, 1.0, 'OUTAGE')"),
                {"t": t},
            )


@pytest.fixture
def trainer_factory(engine, tmp_path):
    def make():
        holder = ModelHolder()
        return Trainer(engine, tmp_path / "models", holder), holder

    return make


# --- training ----------------------------------------------------------------------------------------------------
async def test_train_registers_activates_and_saves_artifact(engine, trainer_factory, tmp_path):
    await seed(engine)
    trainer, holder = trainer_factory()
    report = await trainer.train()
    assert report.activated and report.reason == "no active model" and report.n_rows > 12 * 300
    assert report.mean_mae < report.mean_baseline_mae * 1.05 and len(report.pairs) == 12 and not report.skipped
    assert holder.bundle is not None and holder.bundle.version == report.version and len(holder.bundle.pairs) == 12
    assert (tmp_path / "models" / f"{report.version}.joblib").exists()
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT version, trained_at_tick, mae, rmse, baseline_mae, active, n_rows FROM model_registry"))).all()
    assert len(rows) == 1 and rows[0].active and rows[0].trained_at_tick == TICK
    assert rows[0].mae == pytest.approx(report.mean_mae) and rows[0].baseline_mae == pytest.approx(report.mean_baseline_mae)
    assert REGISTRY.get_sample_value("forecast_mae", {"station": "station-tongi", "fuel": "DIESEL"}) > 0
    assert REGISTRY.get_sample_value("forecast_active_model_trained_at_tick") == TICK
    d = report.to_dict()
    assert d["pairs"]["station-tongi/DIESEL"]["beats_naive_last"] is True and "alpha_grid_mae" not in d["pairs"]["station-tongi/DIESEL"]


async def test_new_version_is_activated_only_if_it_beats_the_active_one(engine, trainer_factory):
    await seed(engine)
    trainer, holder = trainer_factory()
    first = await trainer.train()
    second = await trainer.train()  # identical data: the active model has seen the holdout -> candidate cannot beat it
    assert first.activated and not second.activated and "candidate MAE" in second.reason
    assert holder.bundle.version == first.version and second.previous_version == first.version
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT version, active FROM model_registry ORDER BY created_at"))).all()
    assert [r.active for r in rows] == [True, False] and len(rows) == 2
    # a bundle trained on a very different world must lose to nothing, but must win against a useless active model
    holder.bundle.pairs[("station-tongi", "DIESEL")].pipeline.named_steps["ridge"].coef_[:] = 0
    holder.bundle.pairs[("station-tongi", "DIESEL")].__post_init__()
    holder.bundle.pairs[("station-tongi", "DIESEL")].predictor.intercept = 50.0  # residual model: +50 L on every prediction is a clearly worse model
    third = await trainer.train()
    assert third.activated
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM model_registry WHERE active"))).scalar_one() == 1


async def test_load_active_restores_model_after_restart(engine, trainer_factory):
    await seed(engine)
    trainer, _ = trainer_factory()
    report = await trainer.train()
    fresh, holder = trainer_factory()
    assert holder.bundle is None
    bundle = await fresh.load_active()
    assert bundle is not None and holder.bundle.version == report.version and len(bundle.pairs) == 12
    assert len(await fresh.list_models()) == 1


async def test_load_active_without_registry_or_artifact(engine, trainer_factory, tmp_path):
    trainer, holder = trainer_factory()
    assert await trainer.load_active() is None
    await seed(engine)
    await trainer.train()
    for f in (tmp_path / "models").glob("*.joblib"):
        f.unlink()
    fresh, holder2 = trainer_factory()
    assert await fresh.load_active() is None and holder2.bundle is None  # missing artifact -> fallback mode


async def test_not_enough_data_raises(engine, trainer_factory):
    trainer, _ = trainer_factory()
    with pytest.raises(TrainingError, match="no simulator state"):
        await trainer.train()
    await seed(engine, n_ticks=60)
    with pytest.raises(TrainingError, match="not enough data"):
        await trainer.train()


async def test_outage_rows_are_excluded_from_training(engine, trainer_factory):
    ev = [EventSpec("station_outage", 100, 140, {"station_ids": ["station-tongi"]}, "RESOLVED")]
    await seed(engine, events=ev, outage_ticks=[200, 201, 202])
    trainer, holder = trainer_factory()
    report = await trainer.train()
    tongi = report.pairs["station-tongi/DIESEL"]
    mirpur = report.pairs["station-mirpur/DIESEL"]
    # 41 event ticks + 3 snapshot ticks removed for Tongi only
    assert (mirpur["n_train"] + mirpur["n_val"]) - (tongi["n_train"] + tongi["n_val"]) == 44


def test_should_retrain_schedule():
    class B:  # minimal bundle stand-in
        trained_at_tick = 500

    holder = ModelHolder()
    trainer = Trainer(None, None, holder)  # type: ignore[arg-type]
    assert trainer.should_retrain(300, min_rows=50) is False and trainer.should_retrain(300, min_rows=120) is True
    holder.bundle = B()  # type: ignore[assignment]  # loaded from the registry after a restart
    assert trainer.should_retrain(595, 999) is False and trainer.should_retrain(596, 999) is True  # every 96 ticks
    assert trainer.should_retrain(596, 50) is False  # never with too little data
    assert trainer.should_retrain(100, 50) is False and trainer.should_retrain(100, 120) is True  # simulator reset
    # a completed attempt (even one that did not replace the active model) restarts the 96-tick clock
    trainer.last_train_tick = 100
    assert trainer.should_retrain(100, 120) is False and trainer.should_retrain(195, 120) is False
    assert trainer.should_retrain(196, 120) is True
    assert trainer.should_retrain(50, 120) is True  # another reset


async def test_losing_candidate_does_not_cause_retrain_churn(engine, trainer_factory):
    """Regression (found live): after a simulator reset an old, still-better active model made every candidate lose,
    and the scheduler kept retraining every pass because the *active* model's tick never advanced."""
    await seed(engine)
    trainer, holder = trainer_factory()
    first = await trainer.train()
    assert first.activated and trainer.last_train_tick == TICK
    second = await trainer.train()
    assert not second.activated  # identical data cannot beat the model that has already seen the holdout
    assert holder.bundle.version == first.version and trainer.last_train_tick == TICK
    assert trainer.should_retrain(TICK + 1, 10_000) is False  # the losing attempt still restarts the clock
    assert trainer.should_retrain(TICK + 96, 10_000) is True


# --- state cache ----------------------------------------------------------------------------------------------------------
async def test_state_cache_refresh_reads_everything_the_forecast_needs(engine):
    ev = [EventSpec("demand_spike", 395, 420, {"station_ids": ["station-tongi"], "multiplier": 1.8}, "ACTIVE"),
          EventSpec("demand_spike", 10, 20, {}, "RESOLVED")]  # old and resolved: outside the window
    await seed(engine, events=ev)
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO sim_allocations (id, idempotency_key, destination_station_id, fuel_type, quantity, created_tick, "
            "departure_tick, expected_arrival_tick, status, route_id) VALUES "
            "(1, 'k1', 'station-tongi', 'DIESEL', 4000, 398, 399, 401, 'IN_TRANSIT', 'route-gazipur-tongi'),"
            "(2, 'k2', 'station-mirpur', 'PETROL', 3000, 400, NULL, NULL, 'PENDING', 'route-patiya-mirpur'),"
            "(3, 'k3', 'station-mirpur', 'PETROL', 9999, 300, 301, 303, 'ARRIVED', 'route-patiya-mirpur')"))
    cache = StateCache(engine)
    w = await cache.refresh()
    assert w.tick == TICK and w.tick_minutes == 15 and w.sim_time == sim_time(TICK) and w.sim_status == "RUNNING" and not w.stale
    ps = w.pairs[("station-tongi", "DIESEL")]
    assert ps.inventory == 5000.0 and ps.capacity == 18000.0 and ps.n_rows == TICK  # latest snapshot wins
    assert len(ps.demand_tail) == 96 and list(ps.tail_ticks) == list(range(TICK - 95, TICK + 1))
    assert len(w.pairs) == 12
    kinds = sorted((e.type, e.start_tick) for e in w.events)
    assert kinds == [("demand_spike", 395)]  # RESOLVED spike from tick 10-20 is outside the cache window
    flights = {(a.station_id, a.fuel_type): a for a in w.in_flight}
    assert set(flights) == {("station-tongi", "DIESEL"), ("station-mirpur", "PETROL")}  # ARRIVED excluded
    assert flights[("station-tongi", "DIESEL")].arrival_tick == 401  # expected_arrival_tick as reported
    assert flights[("station-mirpur", "PETROL")].arrival_tick == 400 + 1 + 4  # PENDING: created + 1 + transit(patiya-mirpur = 4)
    assert cache.info()["loaded"] and cache.info()["tick"] == TICK
    v1 = w.version
    assert (await cache.refresh()).version == v1 + 1


async def test_state_cache_flags_stale_and_handles_empty_db(engine):
    cache = StateCache(engine)
    assert await cache.refresh() is None and cache.info() == {"loaded": False}
    await seed(engine, stale=True)
    w = await cache.refresh()
    assert w.stale and all(p.stale for p in w.pairs.values())


async def test_end_to_end_train_then_forecast_from_database(engine, trainer_factory):
    ev = [EventSpec("demand_spike", TICK + 3, TICK + 30, {"station_ids": ["station-tongi"], "multiplier": 1.8}, "SCHEDULED")]
    await seed(engine, events=ev)
    trainer, holder = trainer_factory()
    await trainer.train()
    cache = StateCache(engine)
    await cache.refresh()
    fc = Forecaster(cache, holder, 24)
    body, _ = fc.forecast_json(["station-tongi", "station-mirpur"], ["DIESEL"])
    out = {f["station_id"]: f for f in json.loads(body)["forecasts"]}
    assert all(f["source"] == "MODEL" and f["current_inventory"] == 5000.0 for f in out.values())
    t, m = out["station-tongi"]["horizon"], out["station-mirpur"]["horizon"]
    # Tongi's scheduled spike (ticks 403..430) lifts demand well above the un-spiked Mirpur-style hour profile
    assert t[5]["mean"] > 1.5 * baseline_ratio(t[0], t[5], "station-tongi")
    assert m[0]["tick"] == TICK + 1


def baseline_ratio(p0, p5, station):
    """Expected no-spike growth between step 1 and step 6 from the documented hour factors."""
    from fsp_shared.demand import baseline_demand_at

    h0, h5 = sim_time(p0["tick"]).hour, sim_time(p5["tick"]).hour
    return p0["mean"] * baseline_demand_at(station, "DIESEL", h5, 15) / baseline_demand_at(station, "DIESEL", h0, 15)
