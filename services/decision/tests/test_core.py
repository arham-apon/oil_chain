"""Core decision tests: validator order (every 409 branch), MILP never violates constraints, heuristic, gating."""

import random
from datetime import UTC, datetime

import pytest
from fsp_shared import world_constants as wc
from fsp_shared.breaker import BreakerState
from fsp_shared.config import Settings
from fsp_shared.demand import EventSpec

from app.forecasts import local_forecasts
from app.planner.context import build_context
from app.planner.heuristic import plan_heuristic
from app.planner.milp import solve_milp
from app.triage.jev import Triage, deterministic_urgency
from app.triage.policy import gate
from app.validator import Candidate, shrink_and_validate, validate
from app.whatif import WhatIfOverrides, WhatIfProposal, WhatIfRequest, run_whatif
from app.world import AllocView, DepotView, ReservedView, RouteView, StationView, World


@pytest.fixture
def settings(monkeypatch):
    for k, v in {"DATABASE_URL": "postgresql+asyncpg://x:y@h/d", "TYPESAFE_API_KEY": "", "GEMINI_API_KEY": ""}.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def make_world(tick=40, station_inv=None, events=()):
    depots = {d: DepotView(d, c["region_id"], "OPEN", c["dispatch_capacity_per_tick"], dict(c["initial_inventory"]),
                           dict(c["capacity"])) for d, c in wc.DEPOTS.items()}
    stations = {}
    for s, c in wc.STATIONS.items():
        inv = dict(c["initial_inventory"])
        if station_inv and s in station_inv:
            inv.update(station_inv[s])
        stations[s] = StationView(s, c["region_id"], "OPEN", 1.0, inv, dict(c["capacity"]))
    routes = {r: RouteView(r, c["depot"], c["station"], c["transit_ticks"], c["max_shipment"], "AVAILABLE")
              for r, c in wc.ROUTES.items()}
    return World(tick, datetime(2026, 1, 1, 10, 0, tzinfo=UTC), 15, "RUNNING", False, depots, stations, routes,
                 list(events), [], [], [])


C = Candidate("depot-gazipur", "station-mirpur", "route-gazipur-mirpur", "DIESEL", 3000)


# --- validator: simulator order, first failure wins ---------------------------------------------------------------
def test_validator_every_branch():
    w = make_world()
    assert validate(w, C) == "OK"
    assert validate(w, Candidate("depot-x", "station-mirpur", "route-gazipur-mirpur", "DIESEL", 1)) == "NOT_FOUND"
    assert validate(w, Candidate("depot-patiya", "station-mirpur", "route-gazipur-mirpur", "DIESEL", 1)) == "ROUTE_MISMATCH"
    w.depots["depot-gazipur"].status = "CLOSED"
    assert validate(w, C) == "DEPOT_CLOSED"
    w = make_world()
    w.stations["station-mirpur"].status = "OUTAGE"
    assert validate(w, C) == "STATION_CLOSED"
    w = make_world()
    w.routes["route-gazipur-mirpur"].status = "DISRUPTED"
    assert validate(w, C) == "ROUTE_DISRUPTED"
    w = make_world(events=[EventSpec("route_disruption", 41, 50, {"route_ids": ["route-gazipur-mirpur"]}, "SCHEDULED")])
    assert validate(w, C) == "ROUTE_DISRUPTED"  # disruption covering the departure tick (tick + 1)
    w = make_world()
    assert validate(w, Candidate("depot-gazipur", "station-mirpur", "route-gazipur-mirpur", "DIESEL", 7001)) \
        == "ROUTE_CAPACITY_EXCEEDED"
    w.depots["depot-gazipur"].inventory["DIESEL"] = 2000
    assert validate(w, C) == "INSUFFICIENT_INVENTORY"
    w = make_world()
    w.allocations.append(AllocView(1, "k", "depot-gazipur", "station-tongi", "route-gazipur-tongi", "PETROL", 10000, 40,
                                   None, None, "PENDING"))
    assert validate(w, C) == "DISPATCH_CAPACITY_EXCEEDED"
    w = make_world(station_inv={"station-mirpur": {"DIESEL": 13000}})
    assert validate(w, C) == "DESTINATION_CAPACITY_EXCEEDED"  # 13000 + 3000 > 15000
    w = make_world(station_inv={"station-mirpur": {"DIESEL": 11000}})
    w.allocations.append(AllocView(2, "k2", "depot-patiya", "station-mirpur", "route-patiya-mirpur", "DIESEL", 1000, 30,
                                   31, 35, "IN_TRANSIT"))
    assert validate(w, C) == "ULLAGE_WITH_IN_FLIGHT"  # stricter than the simulator (C5)


def test_constrained_depot_is_shippable_but_derated_and_reservations_count():
    w = make_world()
    w.depots["depot-gazipur"].status = "CONSTRAINED"
    assert validate(w, C, derate=0.5) == "OK"  # 3000 <= 6000
    assert validate(w, Candidate("depot-gazipur", "station-mirpur", "route-gazipur-mirpur", "DIESEL", 6500), derate=0.5) \
        == "DISPATCH_CAPACITY_EXCEEDED"
    w = make_world()
    w.reserved.append(ReservedView("d1", "depot-gazipur", "station-mirpur", "route-gazipur-mirpur", "DIESEL", 5000, "STAGED_REVIEW"))
    assert validate(w, Candidate("depot-gazipur", "station-mirpur", "route-gazipur-mirpur", "DIESEL", 2000)) == "ULLAGE_WITH_IN_FLIGHT"
    assert validate(w, Candidate("depot-gazipur", "station-mirpur", "route-gazipur-mirpur", "DIESEL", 2000,
                                 exclude_decision_id="d1")) == "OK"  # own reservation excluded


def test_shrink_once_to_feasible():
    w = make_world(station_inv={"station-mirpur": {"DIESEL": 12000}})
    cand, code = shrink_and_validate(w, Candidate("depot-gazipur", "station-mirpur", "route-gazipur-mirpur", "DIESEL", 5000))
    assert code == "OK_SHRUNK" and cand.qty == 2700  # 0.98 * 15000 - 12000 = 2700
    cand, code = shrink_and_validate(make_world(), Candidate("depot-gazipur", "station-mirpur", "route-gazipur-tongi", "DIESEL", 5))
    assert cand is None and code == "ROUTE_MISMATCH"  # non-quantity failures are dropped, not shrunk


# --- planners -------------------------------------------------------------------------------------------------------
def low_world(seed):
    rng = random.Random(seed)
    inv = {s: {f: rng.uniform(0.05, 0.9) * wc.STATIONS[s]["capacity"][f] for f in wc.FUEL_TYPES} for s in wc.STATIONS}
    w = make_world(station_inv=inv)
    for d in w.depots.values():
        for f in wc.FUEL_TYPES:
            d.inventory[f] = rng.uniform(0.02, 0.8) * d.capacity[f]
    if rng.random() < 0.5:
        w.routes["route-gazipur-mirpur"].status = "DISRUPTED"
    if rng.random() < 0.5:
        w.depots["depot-patiya"].status = "CONSTRAINED"
    return w


def check_feasible(w, props, settings):
    """Every constraint of spec §11.3 (and the simulator validation) holds for the plan as a whole."""
    per_depot, per_route, per_stock, per_tank = {}, {}, {}, {}
    for p in props:
        assert p.qty >= settings.MIN_LOT_LITERS and p.qty % 10 == 0
        r = w.routes[p.route_id]
        assert r.status == "AVAILABLE" and r.depot_id == p.depot_id and r.station_id == p.station_id
        per_depot[p.depot_id] = per_depot.get(p.depot_id, 0) + p.qty
        per_route[p.route_id] = per_route.get(p.route_id, 0) + p.qty
        per_stock[(p.depot_id, p.fuel)] = per_stock.get((p.depot_id, p.fuel), 0) + p.qty
        per_tank[(p.station_id, p.fuel)] = per_tank.get((p.station_id, p.fuel), 0) + p.qty
    for d, q in per_depot.items():
        dep = w.depots[d]
        assert q <= dep.dispatch_capacity * (settings.DEPOT_CONSTRAINT_DERATE if dep.status == "CONSTRAINED" else 1) + 1e-6
    for r, q in per_route.items():
        assert q <= w.routes[r].max_shipment + 1e-6
    for (d, f), q in per_stock.items():
        assert q <= w.depots[d].inventory[f] + 1e-6
    for (s, f), q in per_tank.items():
        st = w.stations[s]
        assert st.inventory[f] + q <= 0.98 * st.capacity[f] + 1e-6


@pytest.mark.parametrize("seed", range(12))
def test_milp_and_heuristic_never_violate_constraints(seed, settings):
    w = low_world(seed)
    fc = local_forecasts(w, 24)
    ctxs = build_context(w, fc)
    milp = solve_milp(w, ctxs, fc, settings)
    check_feasible(w, milp, settings)
    heur = plan_heuristic(w, ctxs, settings)
    check_feasible(w, heur, settings)
    assert all("route-gazipur-mirpur" != p.route_id for p in milp + heur if w.routes["route-gazipur-mirpur"].status != "AVAILABLE")


def test_milp_reroutes_mirpur_when_primary_route_disrupted(settings):
    w = make_world(station_inv={"station-mirpur": {"DIESEL": 1500}})
    w.routes["route-gazipur-mirpur"].status = "DISRUPTED"
    fc = local_forecasts(w, 24)
    props = solve_milp(w, build_context(w, fc), fc, settings)
    mirpur = [p for p in props if p.station_id == "station-mirpur" and p.fuel == "DIESEL"]
    assert mirpur and all(p.route_id == "route-patiya-mirpur" for p in mirpur)


def test_prestock_before_scheduled_disruption_of_single_route(settings):
    """route-gazipur-tongi disrupted from tick+2: the planner still sees it available at departure (tick+1)."""
    ev = [EventSpec("route_disruption", 42, 70, {"route_ids": ["route-gazipur-tongi"]}, "SCHEDULED")]
    w = make_world(station_inv={"station-tongi": {"DIESEL": 6000}}, events=ev)
    fc = local_forecasts(w, 24)
    props = solve_milp(w, build_context(w, fc), fc, settings)
    assert any(p.route_id == "route-gazipur-tongi" and p.fuel == "DIESEL" for p in props)
    w2 = make_world(station_inv={"station-tongi": {"DIESEL": 6000}},
                    events=[EventSpec("route_disruption", 41, 70, {"route_ids": ["route-gazipur-tongi"]}, "SCHEDULED")])
    fc2 = local_forecasts(w2, 24)
    assert not any(p.station_id == "station-tongi" for p in solve_milp(w2, build_context(w2, fc2), fc2, settings))


def test_full_tanks_produce_no_proposals(settings):
    inv = {s: {f: 0.95 * wc.STATIONS[s]["capacity"][f] for f in wc.FUEL_TYPES} for s in wc.STATIONS}
    w = make_world(station_inv=inv)
    fc = local_forecasts(w, 24)
    assert solve_milp(w, build_context(w, fc), fc, settings) == []


# --- triage + gating -----------------------------------------------------------------------------------------------
def test_deterministic_urgency_levels():
    assert [deterministic_urgency(t, 0.6) for t in (2, 5, 10, 20, 40)] == [5, 4, 3, 2, 1]
    assert deterministic_urgency(None, 0.2) == 3 and deterministic_urgency(None, 0.4) == 2


def g(tri, **kw):
    base = dict(data_stale=False, jev_breaker=BreakerState.CLOSED, jev_enabled=True, forecast_source="MODEL",
                planner="MILP", stockout_risk=0.1, noul_min=0.85, urgency_max=4.5)
    return gate(tri, **{**base, **kw})


def test_gating_policy():
    safe = Triage(2.0, 0.8, None, "nominal", 0.9, 0.95, "JEV")
    assert g(safe) .status == "AUTO_APPROVED" and g(safe).origin == "SYSTEM1_AUTO"
    assert g(safe, data_stale=True).status == "STAGED_REVIEW"  # never autonomous on stale data
    assert g(Triage(4.8, 0.8, None, "x", 0.9, 0.95, "JEV")).status == "STAGED_REVIEW"  # critical
    assert g(Triage(2.0, 0.8, None, "x", 0.9, 0.5, "JEV")).status == "STAGED_REVIEW"  # not confident it's safe
    assert g(Triage(2.0, 0.2, None, "x", 0.9, 0.95, "JEV")).status == "STAGED_REVIEW"  # low urgency confidence
    down = g(Triage(3.0), jev_breaker=BreakerState.OPEN)
    assert down.status == "AUTO_APPROVED" and down.origin == "HEURISTIC_FALLBACK"  # AI down: dispatch continues
    assert g(safe, forecast_source="BASELINE_FALLBACK").origin == "HEURISTIC_FALLBACK"


def test_whatif_never_mutates_and_reports_delta():
    w = make_world(station_inv={"station-mirpur": {"DIESEL": 800}})
    fc = local_forecasts(w, 24)
    before = w.stations["station-mirpur"].inventory["DIESEL"]
    out = run_whatif(w, fc, WhatIfRequest(proposals=[WhatIfProposal(station_id="station-mirpur", fuel_type="DIESEL",
                                                                    qty=5000, route_id="route-gazipur-mirpur")],
                                          overrides=WhatIfOverrides(depot_derate={"depot-gazipur": 0.5})), derate=0.5)
    pair = out["pairs"][0]
    assert out["proposals"][0]["validator"] == "OK" and pair["stockout_risk_after"] < pair["stockout_risk_before"]
    assert w.stations["station-mirpur"].inventory["DIESEL"] == before and not w.reserved and w.depots["depot-gazipur"].dispatch_capacity == 12000
    out2 = run_whatif(w, fc, WhatIfRequest(proposals=[WhatIfProposal(station_id="station-mirpur", fuel_type="DIESEL",
                                                                     qty=7000, route_id="route-gazipur-mirpur")],
                                           overrides=WhatIfOverrides(depot_derate={"depot-gazipur": 0.5})), derate=0.5)
    assert out2["proposals"][0]["validator"] == "DISPATCH_CAPACITY_EXCEEDED"  # 7000 > 12000 x 0.5
