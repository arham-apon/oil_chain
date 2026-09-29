import numpy as np
import pytest

from fsp_shared.demand import EventSpec, baseline_demand_at

from app import features as F
from app.model import MIN_TRAIN_ROWS, ModelBundle, fit_pair, holdout_mae

from .helpers import sim_time, synth_series

S, FUEL = "station-mirpur", "DIESEL"


def trained(n_ticks=480, events=(), seed=7, station=S, fuel=FUEL):
    series = synth_series(station, fuel, n_ticks, events=events, seed=seed)
    frame = F.build_training_frame(station, fuel, series, 15, events)
    return series, frame, fit_pair(station, fuel, frame)


def test_fit_pair_metrics_and_alpha_grid():
    _, frame, pm = trained()
    m = pm.metrics
    assert pm.alpha in (0.01, 0.1, 1.0, 10.0, 100.0, 1e3, 1e4, 1e5) and set(m["alpha_grid_mae"]) == {"0.01", "0.1", "1.0", "10.0", "100.0", "1000.0", "10000.0", "100000.0"}
    assert m["n_train"] + m["n_val"] == len(frame) and m["n_val"] == pytest.approx(0.2 * len(frame), abs=1)
    assert m["val_start_tick"] == int(frame["tick"].iloc[m["n_train"]])  # time-ordered split, never shuffled
    assert m["mae"] == min(m["alpha_grid_mae"].values())
    assert pm.residual_sigma == pytest.approx(m["rmse"]) and pm.sigma_rel > 0


def test_model_beats_last_value_and_matches_the_true_formula():
    """The generator is baseline x mean-1 uniform jitter, so the exact formula is (near) optimal: ridge must
    beat the last-value naive forecast clearly and land within a few percent of the formula."""
    for seed in (1, 2, 3):
        _, _, pm = trained(seed=seed)
        assert pm.metrics["mae"] < pm.metrics["naive_last_mae"] * 0.8
        assert pm.metrics["mae"] <= pm.metrics["naive_formula_mae"] * 1.05


def test_predictor_equals_sklearn_pipeline():
    _, frame, pm = trained()
    x = frame[F.FEATURES].to_numpy()[:50]
    # the sklearn pipeline models the residual on the calendar baseline; the predictor adds the baseline back (D19)
    expected = pm.pipeline.predict(x) + x[:, F.BASELINE_IDX]
    np.testing.assert_allclose(pm.predictor.predict_many(x), expected, rtol=1e-9)
    assert pm.predictor.predict(x[3]) == pytest.approx(expected[3])


def test_too_few_rows_raises():
    series = synth_series(S, FUEL, MIN_TRAIN_ROWS - 10)
    frame = F.build_training_frame(S, FUEL, series, 15)
    with pytest.raises(ValueError, match="usable rows"):
        fit_pair(S, FUEL, frame)


def test_recursive_forecast_shape_nonneg_and_tracks_baseline():
    series, _, pm = trained()
    lag = F.LagState.from_series(series["demand_liters"].to_numpy()[-96:])
    tick = int(series["tick"].iloc[-1])
    pts = pm.forecast(lag, tick, sim_time(tick), 15, 24, [])
    assert [p.tick for p in pts] == list(range(tick + 1, tick + 25))
    assert all(p.mean >= 0 and p.sigma >= 0 for p in pts)
    for p in pts:  # noise-free expectation should follow the documented baseline within ~10 %
        expected = baseline_demand_at(S, FUEL, sim_time(p.tick).hour, 15)
        assert p.mean == pytest.approx(expected, rel=0.10)
    assert pts[0].sigma == pytest.approx(pm.sigma_rel * pts[0].mean)
    # the caller's lag state is left untouched
    assert lag.last5 == [float(v) for v in series["demand_liters"].to_numpy()[-5:]]


def test_forecast_is_deterministic():
    series, _, pm = trained()
    tick = int(series["tick"].iloc[-1])
    a = pm.forecast(F.LagState.from_series(series["demand_liters"].to_numpy()[-96:]), tick, sim_time(tick), 15, 12, [])
    b = pm.forecast(F.LagState.from_series(series["demand_liters"].to_numpy()[-96:]), tick, sim_time(tick), 15, 12, [])
    assert [p.mean for p in a] == [p.mean for p in b]


def test_scheduled_spike_scales_the_forecast_before_it_starts():
    """Scheduled events are visible ahead of time: the multiplier feature lifts exactly the covered ticks."""
    ev_train = [EventSpec("demand_spike", 200, 230, {"station_ids": [S], "multiplier": 1.8}),
                EventSpec("demand_spike", 330, 350, {"station_ids": [S], "multiplier": 1.4})]
    series, _, pm = trained(n_ticks=480, events=ev_train)
    tick = int(series["tick"].iloc[-1])
    lag = F.LagState.from_series(series["demand_liters"].to_numpy()[-96:])
    plain = pm.forecast(lag, tick, sim_time(tick), 15, 24, [])
    spike = [EventSpec("demand_spike", tick + 6, tick + 10, {"station_ids": [S], "multiplier": 1.8})]
    spiked = pm.forecast(lag, tick, sim_time(tick), 15, 24, spike)
    ratios = {p.tick - tick: s.mean / p.mean for p, s in zip(plain, spiked, strict=True)}
    for k in range(1, 6):
        assert ratios[k] == pytest.approx(1.0, abs=0.02)  # before the spike: unaffected (lags are the same)
    for k in range(6, 11):
        assert ratios[k] > 1.5, (k, ratios[k])  # inside [start, end] inclusive: lifted towards 1.8x
    assert ratios[10] > 1.5 and 0.9 < ratios[13] < 1.35  # end tick inclusive; afterwards it decays via the EWMA lags


def test_model_learns_multiplier_from_events():
    ev = [EventSpec("demand_spike", 200, 240, {"station_ids": [S], "multiplier": 1.8})]
    _, frame, pm = trained(n_ticks=480, events=ev)
    # the model predicts multiplier-normalised demand; the known multiplier scales it back (D18)
    spike_rows = frame[(frame["tick"] >= 200) & (frame["tick"] <= 240)]
    pred = pm.predictor.predict_many(spike_rows[F.FEATURES].to_numpy()) * spike_rows["demand_multiplier"].to_numpy()
    rel_err = np.abs(pred - spike_rows["y"].to_numpy()) / spike_rows["y"].to_numpy()
    assert rel_err.mean() < 0.12


def test_save_load_roundtrip(tmp_path):
    series, frame, pm = trained()
    bundle = ModelBundle("ridge-test", 480, 15, {(S, FUEL): pm}, n_rows=len(frame))
    path = tmp_path / "m" / "bundle.joblib"
    bundle.save(path)
    loaded = ModelBundle.load(path)
    assert loaded.version == "ridge-test" and loaded.get(S, FUEL) is not None and loaded.get("x", FUEL) is None
    x = frame[F.FEATURES].to_numpy()[:20]
    np.testing.assert_allclose(loaded.get(S, FUEL).predictor.predict_many(x), pm.predictor.predict_many(x))
    assert loaded.mean_mae == pytest.approx(pm.metrics["mae"])
    assert loaded.mean_baseline_mae == pytest.approx(min(pm.metrics["naive_last_mae"], pm.metrics["naive_formula_mae"]))
    with pytest.raises(TypeError):
        import joblib

        joblib.dump({"not": "a bundle"}, tmp_path / "x.joblib")
        ModelBundle.load(tmp_path / "x.joblib")


def test_holdout_mae_compares_models_on_the_same_slice():
    _, frame, pm = trained()
    assert holdout_mae(pm, frame, pm.metrics["val_start_tick"]) < pm.metrics["naive_last_mae"]
    assert holdout_mae(pm, frame, 10**9) == float("inf")


def test_heavy_shrinkage_falls_back_to_the_calendar_formula():
    """With a huge alpha the residual model predicts ~0, i.e. exactly the documented formula (D19)."""
    from app.model import make_pipeline

    series = synth_series(S, FUEL, 300)
    frame = F.build_training_frame(S, FUEL, series, 15)
    pipe = make_pipeline(1e9).fit(frame[F.FEATURES].to_numpy(), (frame["y_norm"] - frame["baseline"]).to_numpy())
    resid = pipe.predict(frame[F.FEATURES].to_numpy())
    assert np.abs(resid).max() < 0.05 * frame["baseline"].mean()
