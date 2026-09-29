import math

import pytest

from fsp_shared import world_constants as wc
from fsp_shared.demand import (
    EventSpec,
    baseline_demand_at,
    baseline_horizon,
    covers_entity,
    noise_of,
    spike_active,
    spike_multiplier,
    station_outage,
)
from fsp_shared.risk import compute_risk, normal_sf
from fsp_shared.schemas import HorizonPoint


# --- events ----------------------------------------------------------------------------------------------
def spike(start, end, **params):
    return EventSpec("demand_spike", start, end, params)


def test_event_end_tick_is_inclusive():
    """Verified against the simulator: a spike starting at 300 with duration 24 (end_tick 324) affects tick 324."""
    ev = [spike(300, 324, station_ids=["station-tongi"], multiplier=1.8)]
    assert spike_multiplier("station-tongi", 299, ev) == 1.0
    assert spike_multiplier("station-tongi", 300, ev) == 1.8
    assert spike_multiplier("station-tongi", 324, ev) == 1.8
    assert spike_multiplier("station-tongi", 325, ev) == 1.0
    assert spike_active("station-tongi", 310, ev) and not spike_active("station-tongi", 325, ev)


def test_event_filters_station_region_and_all():
    assert spike_multiplier("station-mirpur", 5, [spike(0, 10, station_ids=["station-tongi"], multiplier=2)]) == 1.0
    assert spike_multiplier("station-tongi", 5, [spike(0, 10, region_ids=["region-dhaka"], multiplier=2)]) == 2.0
    assert spike_multiplier("station-coxsbazar", 5, [spike(0, 10, region_ids=["region-dhaka"], multiplier=2)]) == 1.0
    assert spike_multiplier("station-coxsbazar", 5, [spike(0, 10, multiplier=2)]) == 2.0  # empty filters = all
    assert spike_multiplier("station-coxsbazar", 5, [spike(0, 10)]) == 1.5  # simulator default multiplier
    both = [spike(0, 10, region_ids=["region-dhaka"], multiplier=2), spike(5, 8, station_ids=["station-tongi"], multiplier=1.5)]
    assert spike_multiplier("station-tongi", 6, both) == pytest.approx(3.0)  # overlapping spikes multiply


def test_other_event_types_do_not_scale_demand_but_outage_is_detected():
    ev = [EventSpec("station_outage", 10, 20, {"station_ids": ["station-tongi"]}), EventSpec("route_disruption", 0, 99, {})]
    assert spike_multiplier("station-tongi", 12, ev) == 1.0
    assert station_outage("station-tongi", 12, ev) and not station_outage("station-mirpur", 12, ev)
    assert not station_outage("station-tongi", 21, ev)
    assert covers_entity(EventSpec("x", 0, 1, {}), "station-tongi", "region-dhaka", id_key="station_ids")


# --- baseline horizon ---------------------------------------------------------------------------------------
def test_baseline_horizon_uses_projected_hour_and_spikes():
    # tick 0 = 2026-01-01T00:00; industrial Tongi flips from off-peak to peak at 06:00 (tick 24)
    pts = baseline_horizon("station-tongi", "DIESEL", 22, "2026-01-01T05:30:00", 15, 6, [])
    by_tick = {p.tick: p for p in pts}
    off = baseline_demand_at("station-tongi", "DIESEL", 5, 15)
    peak = baseline_demand_at("station-tongi", "DIESEL", 6, 15)
    assert by_tick[23].mean == pytest.approx(off) and by_tick[24].mean == pytest.approx(peak)
    assert peak / off == pytest.approx(1.55 / 0.45)
    assert by_tick[23].sigma == pytest.approx(noise_of("station-tongi") * off)  # spec 10.6: sigma = noise x baseline

    spiked = baseline_horizon("station-tongi", "DIESEL", 22, "2026-01-01T05:30:00", 15, 6, [spike(24, 25, station_ids=["station-tongi"], multiplier=1.8)])
    assert spiked[1].mean == pytest.approx(peak * 1.8) and spiked[0].mean == pytest.approx(off)
    assert [p.tick for p in pts] == [23, 24, 25, 26, 27, 28]


def test_baseline_matches_daily_volume():
    """Summing a full day of baseline (mult 1) reproduces the documented liters/day x mean hour factor."""
    total = sum(baseline_demand_at("station-mirpur", "DIESEL", (t * 15 // 60) % 24, 15) for t in range(96))
    factors = [wc.hour_factor("urban_high", h) for h in range(24)]
    assert total == pytest.approx(8500 * sum(factors) / 24 * 1.0)


# --- risk ------------------------------------------------------------------------------------------------------
def horizon(means, sigma=0.0, start=10):
    return [HorizonPoint(tick=start + i + 1, mean=m, sigma=sigma) for i, m in enumerate(means)]


def test_burn_rate_uses_tick_minutes():
    r = compute_risk(1e6, horizon([100.0, 100.0]), 10, 15)
    assert r.burn_rate_lph == pytest.approx(400.0)  # 100 L/tick x 4 ticks/h
    assert compute_risk(1e6, horizon([100.0]), 10, 30).burn_rate_lph == pytest.approx(200.0)


def test_time_to_empty_deterministic():
    r = compute_risk(250.0, horizon([100.0] * 5), 10, 15)
    assert r.t_empty_ticks == 3 and r.t_empty_hours == pytest.approx(0.75)  # 250-300 <= 0 at k=3
    assert r.stockout_risk == 1.0 and r.min_projected_inventory == pytest.approx(-250.0)
    safe = compute_risk(10_000.0, horizon([100.0] * 5), 10, 15)
    assert safe.t_empty_ticks is None and safe.t_empty_hours is None and safe.stockout_risk == 0.0


def test_arrivals_push_out_empty_and_are_counted_from_their_tick():
    base = compute_risk(250.0, horizon([100.0] * 6), 10, 15)
    assert base.t_empty_ticks == 3
    with_arrival = compute_risk(250.0, horizon([100.0] * 6), 10, 15, [(13, 1000.0)])  # arrives at k=3
    assert with_arrival.t_empty_ticks is None
    late = compute_risk(250.0, horizon([100.0] * 6), 10, 15, [(15, 1000.0)])  # arrives at k=5, too late for k=3
    assert late.t_empty_ticks == 3


def test_stockout_risk_gaussian():
    # inventory exactly equals expected cumulative demand at k=4 -> P(C_4 > I) = 0.5
    r = compute_risk(400.0, horizon([100.0] * 4, sigma=20.0), 10, 15)
    assert r.stockout_risk == pytest.approx(0.5, abs=1e-9)
    # one sigma of headroom at k=1 only matters if it is the worst step
    r1 = compute_risk(120.0, horizon([100.0], sigma=20.0), 10, 15)
    assert r1.stockout_risk == pytest.approx(normal_sf(1.0))
    # variance accumulates as k x sigma^2
    r2 = compute_risk(230.0, horizon([100.0, 100.0], sigma=10.0), 10, 15)
    assert r2.stockout_risk == pytest.approx(normal_sf(30.0 / (10.0 * math.sqrt(2))))


def test_risk_is_monotone_in_inventory_and_empty_horizon():
    hz = horizon([100.0] * 8, sigma=15.0)
    risks = [compute_risk(inv, hz, 10, 15).stockout_risk for inv in (100, 400, 800, 1200)]
    assert risks == sorted(risks, reverse=True) and risks[-1] < 0.01 and risks[0] > 0.99
    assert compute_risk(10.0, [], 10, 15).burn_rate_lph == 0.0
