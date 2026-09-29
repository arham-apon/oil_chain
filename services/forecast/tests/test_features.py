import numpy as np
import pandas as pd
import pytest
from fsp_shared.demand import EventSpec, baseline_demand_at

from app import features as F

from .helpers import sim_time, synth_series

S, FUEL = "station-tongi", "DIESEL"


def frame_for(series, events=(), outage=()):
    return F.build_training_frame(S, FUEL, series, 15, events, outage)


def test_feature_names_match_spec():
    assert F.FEATURES == [
        "sin_hour", "cos_hour", "ewma_4", "ewma_16", "momentum_4",
        "demand_multiplier", "event_active", "baseline", "hour_factor",
    ]


def test_frame_values_match_definitions():
    series = synth_series(S, FUEL, 200)
    fr = frame_for(series)
    d = series.set_index("tick")["demand_liters"]
    row = fr[fr["tick"] == 100].iloc[0]
    st = sim_time(100)
    assert row["sin_hour"] == pytest.approx(np.sin(2 * np.pi * st.hour / 24))
    assert row["cos_hour"] == pytest.approx(np.cos(2 * np.pi * st.hour / 24))
    assert row["momentum_4"] == pytest.approx(d[99] - d[95])
    assert row["baseline"] == pytest.approx(baseline_demand_at(S, FUEL, st.hour, 15))
    assert row["hour_factor"] == 0.45 and row["y"] == pytest.approx(d[100])
    assert row["naive_last"] == pytest.approx(d[99]) and row["naive_formula"] == pytest.approx(row["baseline"])
    # EWMAs only look at d(<= t-1)
    e4 = e16 = d[1]
    for t in range(2, 100):
        e4 = F.ALPHA_4 * d[t] + (1 - F.ALPHA_4) * e4
        e16 = F.ALPHA_16 * d[t] + (1 - F.ALPHA_16) * e16
    assert row["ewma_4"] == pytest.approx(e4) and row["ewma_16"] == pytest.approx(e16)


def test_warmup_rows_are_dropped_and_no_nans():
    fr = frame_for(synth_series(S, FUEL, 120))
    assert fr["tick"].min() == 1 + F.WARMUP_ROWS and len(fr) == 120 - F.WARMUP_ROWS
    assert not fr[F.FEATURES + ["y"]].isna().any().any()


def test_no_target_leakage_features_at_t_ignore_d_t_and_future():
    series = synth_series(S, FUEL, 150)
    base = frame_for(series)
    tampered = series.copy()
    idx = tampered.index[tampered["tick"] >= 80]
    tampered.loc[idx, "demand_liters"] *= 10  # change d(80) and everything after
    other = frame_for(tampered)
    before = base[base["tick"] <= 80][F.FEATURES]
    after = other[other["tick"] <= 80][F.FEATURES]
    pd.testing.assert_frame_equal(before.reset_index(drop=True), after.reset_index(drop=True))  # rows <= 80 unchanged
    assert not np.allclose(base[base["tick"] == 81][F.FEATURES], other[other["tick"] == 81][F.FEATURES])  # d(80) is a lag for 81
    assert (other[other["tick"] >= 80]["y"].to_numpy() != base[base["tick"] >= 80]["y"].to_numpy()).all()


def test_inference_features_equal_training_features():
    """The step-by-step inference path must reproduce the vectorised training frame exactly."""
    series = synth_series(S, FUEL, 220)
    fr = frame_for(series)
    d = series["demand_liters"].to_numpy()
    for tick in (60, 131, 200):
        prefix = series[series["tick"] < tick]["demand_liters"].to_numpy()
        lag = F.LagState.from_series(prefix)
        x = F.feature_row(S, FUEL, sim_time(tick).hour, 15, 1.0, False, lag.e4, lag.e16, lag.d_prev1(), lag.d_prev5())
        expected = fr[fr["tick"] == tick][F.FEATURES].to_numpy()[0]
        np.testing.assert_allclose(x, expected, rtol=1e-9)
        assert d[tick - 1] == pytest.approx(series[series["tick"] == tick]["demand_liters"].iloc[0])


def test_lag_state_tail_converges_to_full_history():
    d = synth_series(S, FUEL, 400)["demand_liters"].to_numpy()
    full, tail = F.LagState.from_series(d), F.LagState.from_series(d[-96:])
    assert tail.e4 == pytest.approx(full.e4, rel=1e-6) and tail.e16 == pytest.approx(full.e16, rel=1e-3)


def test_lag_state_push_and_validation():
    lag = F.LagState.from_series([1, 2, 3, 4, 5, 6])
    assert (lag.d_prev1(), lag.d_prev5()) == (6, 2)
    lag.push(7)
    assert (lag.d_prev1(), lag.d_prev5()) == (7, 3) and lag.last5 == [3, 4, 5, 6, 7]
    with pytest.raises(ValueError):
        F.LagState.from_series([1, 2, 3])


def test_spike_multiplier_and_event_active_use_inclusive_end():
    ev = [EventSpec("demand_spike", 100, 110, {"station_ids": [S], "multiplier": 1.8})]
    fr = frame_for(synth_series(S, FUEL, 200, events=ev), ev)
    m = fr.set_index("tick")
    assert m.loc[99, "demand_multiplier"] == 1.0 and m.loc[100, "demand_multiplier"] == 1.8
    assert m.loc[110, "demand_multiplier"] == 1.8 and m.loc[111, "demand_multiplier"] == 1.0
    assert m.loc[105, "event_active"] == 1.0 and m.loc[111, "event_active"] == 0.0
    # D18: the model feature `baseline` is multiplier-free; the multiplier lives in its own feature, the
    # normalised target, and in the naive formula predictor used for scoring
    base = m.loc[105, "hour_factor"] * 14000 * 15 / 1440
    assert m.loc[105, "baseline"] == pytest.approx(base) and m.loc[105, "naive_formula"] == pytest.approx(base * 1.8)
    assert m.loc[105, "y_norm"] == pytest.approx(m.loc[105, "y"] / 1.8)


def test_outage_rows_are_flagged_from_events_and_snapshots():
    ev = [EventSpec("station_outage", 50, 60, {"station_ids": [S]})]
    fr = frame_for(synth_series(S, FUEL, 120), ev, outage=[90, 91]).set_index("tick")
    assert fr.loc[55, "outage"] and fr.loc[60, "outage"] and fr.loc[90, "outage"] and not fr.loc[70, "outage"]


def test_missing_ticks_are_never_targets_and_lags_survive():
    fr = frame_for(synth_series(S, FUEL, 120, drop_ticks={60, 61}))
    assert 60 not in set(fr["tick"]) and 61 not in set(fr["tick"]) and 62 in set(fr["tick"])
    assert not fr[F.FEATURES].isna().any().any()


def test_hours_follow_sim_time_across_days():
    fr = frame_for(synth_series(S, FUEL, 300)).set_index("tick")
    assert fr.loc[97, "hour_factor"] == fr.loc[1 + 96, "hour_factor"] == 0.45  # same hour next day (00:00)
    assert fr.loc[24 + 96, "hour_factor"] == 1.55  # 06:00 on day 2


def test_lags_are_computed_on_multiplier_normalised_demand():
    """During a spike the EWMAs must not jump by the multiplier (D18): they track the normalised level."""
    ev = [EventSpec("demand_spike", 100, 130, {"station_ids": [S], "multiplier": 2.0})]
    with_spike = frame_for(synth_series(S, FUEL, 200, events=ev), ev).set_index("tick")
    plain = frame_for(synth_series(S, FUEL, 200)).set_index("tick")
    ratio = with_spike.loc[125, "ewma_16"] / plain.loc[125, "ewma_16"]
    assert 0.8 < ratio < 1.25  # would be ~2.0 if lags used raw demand
