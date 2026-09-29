"""Deterministic (s,S) heuristic fallback (spec §11.4). Must stay well under 5 ms for 12 pairs.

    s_reorder = mean_per_tick x (transit_ticks(r) + 2)        S_target = 0.90 x capacity
    if projected_inventory_at_arrival <= s_reorder:  Q = min(S_target - inventory - F, max_shipment, U, B remaining)
"""

from __future__ import annotations

from fsp_shared.config import Settings

from ..world import World
from .context import PairCtx, Proposal, depot_budget, usable_stock

TARGET_FILL = 0.90
ULLAGE_FRACTION = 0.98
FLOOR_L = 10.0


def plan_heuristic(world: World, ctxs: dict[tuple[str, str], PairCtx], settings: Settings) -> list[Proposal]:
    derate, min_lot = settings.DEPOT_CONSTRAINT_DERATE, float(settings.MIN_LOT_LITERS)
    budgets = {d: depot_budget(world, d, derate) for d in world.depots}
    stock: dict[tuple[str, str], float] = {}
    route_used: dict[str, float] = {}
    per_route = settings.ROUTE_CAP_MODE == "per_route_per_cycle"
    order = sorted(ctxs.values(), key=lambda c: (c.risk.t_empty_ticks is None, c.risk.t_empty_ticks or 0))  # most urgent first
    out: list[Proposal] = []
    for c in order:
        r = min(c.routes, key=lambda rt: rt.transit_ticks)  # shortest available route
        if c.projected_at_arrival > c.mean_per_tick * (r.transit_ticks + 2) * (1 + 1e-12):
            continue
        key = (r.depot_id, c.fuel)
        stock.setdefault(key, usable_stock(world, r.depot_id, c.fuel))
        s_target = TARGET_FILL * c.capacity
        headroom = max(0.0, ULLAGE_FRACTION * c.capacity - c.inventory - c.in_flight)
        cap = r.max_shipment - (route_used.get(r.id, 0.0) if per_route else 0.0)
        q = min(s_target - c.inventory - c.in_flight, cap, stock[key], budgets[r.depot_id], headroom)
        q = float(int(q // FLOOR_L) * FLOOR_L)
        if q < min_lot:
            continue
        stock[key] -= q
        budgets[r.depot_id] -= q
        route_used[r.id] = route_used.get(r.id, 0.0) + q
        out.append(Proposal(r.id, r.depot_id, c.station_id, c.fuel, q, "HEURISTIC", [], c.need, c.weight))
    return out
