"""What-if simulation (spec §11.11). Pure computation over a copy of the world — never POSTs."""

from __future__ import annotations

from typing import Any

from fsp_shared.risk import compute_risk
from fsp_shared.schemas import HorizonPoint, PairForecast
from pydantic import BaseModel, Field

from .forecasts import arrivals_for
from .validator import Candidate, validate
from .world import World


class WhatIfProposal(BaseModel):
    station_id: str
    fuel_type: str
    qty: float = Field(gt=0)
    route_id: str


class WhatIfOverrides(BaseModel):
    depot_derate: dict[str, float] = Field(default_factory=dict)  # depot_id -> factor on dispatch capacity (0..1)
    route_disabled: list[str] = Field(default_factory=list)
    demand_multiplier: dict[str, float] = Field(default_factory=dict)  # station_id -> extra multiplier


class WhatIfRequest(BaseModel):
    proposals: list[WhatIfProposal] = Field(default_factory=list)
    overrides: WhatIfOverrides = Field(default_factory=WhatIfOverrides)
    horizon: int = Field(default=24, ge=1, le=96)


def apply_overrides(world: World, ov: WhatIfOverrides) -> World:
    w = world.copy()
    for d, f in ov.depot_derate.items():
        if d in w.depots:
            w.depots[d].dispatch_capacity *= max(0.0, min(1.0, f))
    for r in ov.route_disabled:
        if r in w.routes:
            w.routes[r].status = "DISRUPTED"
    return w


def run_whatif(world: World, forecasts: dict[tuple[str, str], PairForecast], req: WhatIfRequest,
               *, derate: float) -> dict[str, Any]:
    w = apply_overrides(world, req.overrides)
    results: list[dict[str, Any]] = []
    validations = []
    extra_arrivals: dict[tuple[str, str], list[tuple[int, float]]] = {}
    depot_used: dict[tuple[str, str], float] = {}
    for p in req.proposals:
        r = w.routes.get(p.route_id)
        depot_id = r.depot_id if r else ""
        code = validate(w, Candidate(depot_id, p.station_id, p.route_id, p.fuel_type, p.qty), derate=derate)
        validations.append({**p.model_dump(), "depot_id": depot_id, "validator": code})
        if code == "OK" and r is not None:
            extra_arrivals.setdefault((p.station_id, p.fuel_type), []).append((w.tick + 1 + r.transit_ticks, p.qty))
            depot_used[(depot_id, p.fuel_type)] = depot_used.get((depot_id, p.fuel_type), 0.0) + p.qty
            # later proposals see this one as reserved (dispatch/ullage/stock)
            from .world import ReservedView

            w.reserved.append(ReservedView(f"whatif-{len(w.reserved)}", depot_id, p.station_id, p.route_id,
                                           p.fuel_type, p.qty, "WHATIF"))

    affected = {(p.station_id, p.fuel_type) for p in req.proposals} | {
        (s, f) for (s, f) in forecasts if s in req.overrides.demand_multiplier
    }
    if not affected:
        affected = set(forecasts)
    for key in sorted(affected):
        pf = forecasts.get(key)
        st = w.stations.get(key[0])
        if pf is None or st is None:
            continue
        m = req.overrides.demand_multiplier.get(key[0], 1.0)
        hz = [HorizonPoint(tick=p.tick, mean=p.mean * m, sigma=p.sigma * m) for p in pf.horizon[: req.horizon]]
        inv = st.inventory[key[1]]
        base_arr = arrivals_for(w, *key)
        before = compute_risk(inv, hz, w.tick, w.tick_minutes, base_arr)
        after = compute_risk(inv, hz, w.tick, w.tick_minutes, base_arr + extra_arrivals.get(key, []))
        curve, cum, arr_all = [], 0.0, sorted(base_arr + extra_arrivals.get(key, []))
        for p in hz:
            cum += p.mean
            curve.append({"tick": p.tick, "projected_inventory": inv + sum(q for t, q in arr_all if t <= p.tick) - cum})
        results.append({
            "station_id": key[0], "fuel_type": key[1], "current_inventory": inv,
            "stockout_risk_before": before.stockout_risk, "stockout_risk_after": after.stockout_risk,
            "t_empty_before": before.t_empty_ticks, "t_empty_after": after.t_empty_ticks, "curve": curve,
        })
    depots = {
        f"{d}/{f}": {"inventory": w.depots[d].inventory[f], "used_by_whatif": q,
                     "remaining": w.depots[d].inventory[f] - q}
        for (d, f), q in depot_used.items()
    }
    return {"tick": w.tick, "proposals": validations, "pairs": results, "depots": depots,
            "overrides": req.overrides.model_dump(), "note": "simulation only - nothing was dispatched"}
