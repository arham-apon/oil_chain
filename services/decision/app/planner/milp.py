"""MILP planner (spec §11.3, PuLP + CBC, ``timeLimit=1``). Written with explicit index maps (C14: no walrus).

min  Σ w(s,f)·u[s,f] + Σ c_transit·transit(r)·x[r,f] + Σ c_reserve·v[d,f]
s.t. 1 coverage      Σ_{r→s} x[r,f] + u[s,f] ≥ N(s,f)                     (u = unmet-need slack, C13)
     2 depot stock   Σ_{r from d} x[r,f] ≤ U(d,f)
     3 dispatch      Σ_{r from d, f} x[r,f] ≤ B(d)
     4 route cap     Σ_f x[r,f] ≤ max_shipment(r)      (ROUTE_CAP_MODE=per_route_per_cycle; else per allocation)
     5 min lot       MIN_LOT·y ≤ x ≤ max_shipment·y
     6 ullage        inv + F + Σ_{r→s} x ≤ 0.98·cap   (stricter than the simulator, C5)
     7 depot reserve U(d,f) − Σ x + v[d,f] ≥ R(d,f)     (v = reserve-shortfall slack)
"""

from __future__ import annotations

import pulp
from fsp_shared.config import Settings
from fsp_shared.schemas import PairForecast

from ..world import World
from .context import (
    ULLAGE_FRACTION,
    PairCtx,
    Proposal,
    depot_budget,
    depot_reserve,
    usable_stock,
)

C_TRANSIT = 0.01
C_RESERVE = 5.0
FLOOR_L = 10.0


class PlanningError(Exception):
    pass


def solve_milp(
    world: World,
    ctxs: dict[tuple[str, str], PairCtx],
    forecasts: dict[tuple[str, str], PairForecast],
    settings: Settings,
    *,
    time_limit: float = 1.0,
) -> list[Proposal]:
    active = {k: c for k, c in ctxs.items() if c.need > 0}
    if not active:
        return []
    derate = settings.DEPOT_CONSTRAINT_DERATE
    min_lot = float(settings.MIN_LOT_LITERS)
    per_route = settings.ROUTE_CAP_MODE == "per_route_per_cycle"

    prob = pulp.LpProblem("fuel_dispatch", pulp.LpMinimize)
    # index maps
    opts: list[tuple[str, str, str, str]] = []  # (route_id, depot_id, station_id, fuel)
    for (sid, fuel), c in active.items():
        for r in c.routes:
            opts.append((r.id, r.depot_id, sid, fuel))
    x = {o: pulp.LpVariable(f"x_{i}", lowBound=0) for i, o in enumerate(opts)}
    y = {o: pulp.LpVariable(f"y_{i}", cat=pulp.LpBinary) for i, o in enumerate(opts)}
    u = {k: pulp.LpVariable(f"u_{i}", lowBound=0) for i, k in enumerate(active)}
    depot_fuels = sorted({(o[1], o[3]) for o in opts})
    v = {k: pulp.LpVariable(f"v_{i}", lowBound=0) for i, k in enumerate(depot_fuels)}

    def ship_of(route_id: str) -> float:
        return world.routes[route_id].max_shipment

    def transit_of(route_id: str) -> int:
        return world.routes[route_id].transit_ticks

    prob += (
        pulp.lpSum(active[k].weight * u[k] for k in active)
        + pulp.lpSum(C_TRANSIT * transit_of(o[0]) * x[o] for o in opts)
        + pulp.lpSum(C_RESERVE * v[k] for k in depot_fuels)
    )
    for k, c in active.items():  # 1 coverage
        prob += pulp.lpSum(x[o] for o in opts if (o[2], o[3]) == k) + u[k] >= c.need, f"cover_{k[0]}_{k[1]}"
    for k in depot_fuels:  # 2 stock, 7 reserve
        d, f = k
        used = pulp.lpSum(x[o] for o in opts if (o[1], o[3]) == k)
        stock = usable_stock(world, d, f)
        prob += used <= stock, f"stock_{d}_{f}"
        prob += stock - used + v[k] >= depot_reserve(world, d, f, forecasts), f"reserve_{d}_{f}"
    budgets = {d: depot_budget(world, d, derate) for d in {o[1] for o in opts}}
    for d, b in budgets.items():  # 3 dispatch
        prob += pulp.lpSum(x[o] for o in opts if o[1] == d) <= b, f"dispatch_{d}"
    for rid in {o[0] for o in opts}:  # 4 route cap
        if per_route:
            prob += pulp.lpSum(x[o] for o in opts if o[0] == rid) <= ship_of(rid), f"route_{rid}"
    for i, o in enumerate(opts):  # 5 linking / min lot / per-allocation cap
        # per_allocation mode: a route may carry several max-size allocations per cycle (chunked at execution)
        prob += x[o] <= ship_of(o[0]) * (1 if per_route else 3) * y[o], f"link_hi_{i}"
        prob += x[o] >= min_lot * y[o], f"link_lo_{i}"
    for k, c in active.items():  # 6 ullage (headroom floored at 0 so the model stays feasible when already full)
        headroom = max(0.0, ULLAGE_FRACTION * c.capacity - c.inventory - c.in_flight)
        prob += pulp.lpSum(x[o] for o in opts if (o[2], o[3]) == k) <= headroom, f"ullage_{k[0]}_{k[1]}"

    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit))
    status = pulp.LpStatus[prob.status]
    if status not in ("Optimal",) or any(var.value() is None for var in x.values()):
        raise PlanningError(f"MILP status {status}")

    proposals: list[Proposal] = []
    for o in opts:
        qty = (x[o].value() or 0.0)
        qty = float(int(qty // FLOOR_L) * FLOOR_L)  # floor to multiples of 10 L
        if qty < min_lot:
            continue
        rid, d, sid, fuel = o
        c = active[(sid, fuel)]
        binding = _binding(world, o, qty, c, budgets, opts, x, u, v, per_route, forecasts)
        proposals.append(Proposal(rid, d, sid, fuel, qty, "MILP", binding, c.need, c.weight))
    return proposals


def _binding(world, o, qty, c: PairCtx, budgets, opts, x, u, v, per_route, forecasts) -> list[str]:
    """Constraints whose slack is <= 1 L at the solution (reused in explanations)."""
    rid, d, sid, fuel = o
    out = []
    route_total = sum((x[p].value() or 0.0) for p in opts if p[0] == rid) if per_route else (x[o].value() or 0.0)
    if world.routes[rid].max_shipment - route_total <= 1.0:
        out.append("route_max_shipment")
    used_stock = sum((x[p].value() or 0.0) for p in opts if (p[1], p[3]) == (d, fuel))
    if usable_stock(world, d, fuel) - used_stock <= 1.0:
        out.append("depot_stock")
    if budgets[d] - sum((x[p].value() or 0.0) for p in opts if p[1] == d) <= 1.0:
        out.append("depot_dispatch_capacity")
    tank_sum = sum((x[p].value() or 0.0) for p in opts if (p[2], p[3]) == (sid, fuel))
    if max(0.0, ULLAGE_FRACTION * c.capacity - c.inventory - c.in_flight) - tank_sum <= 1.0:
        out.append("station_ullage")
    if (u[(sid, fuel)].value() or 0.0) > 1.0:
        out.append("need_not_fully_covered")
    if (v[(d, fuel)].value() or 0.0) > 1.0:
        out.append("depot_reserve_shortfall")
    return out
