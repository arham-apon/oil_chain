"""Planning inputs shared by the MILP and the heuristic (spec §11.2)."""

from __future__ import annotations

from dataclasses import dataclass, field

from fsp_shared import world_constants as wc
from fsp_shared.risk import compute_risk
from fsp_shared.schemas import PairForecast, RiskMetrics

from ..validator import route_disrupted_at
from ..world import RouteView, World

COVER_TICKS = 32
TARGET_FILL = 0.90
ULLAGE_FRACTION = 0.98


@dataclass
class Proposal:
    route_id: str
    depot_id: str
    station_id: str
    fuel: str
    qty: float
    origin: str = "MILP"  # MILP | HEURISTIC
    binding_constraints: list[str] = field(default_factory=list)
    need: float = 0.0
    weight: float = 0.0


@dataclass
class PairCtx:
    station_id: str
    fuel: str
    routes: list[RouteView]  # AVAILABLE, not covered by a scheduled/active disruption at departure
    inventory: float
    capacity: float
    in_flight: float  # F(s,f)
    means: list[float]
    risk: RiskMetrics
    mean_per_tick: float
    l_min: int
    target: float
    safety_stock: float
    projected_at_arrival: float
    need: float
    weight: float
    forecast: PairForecast


def cum_demand(means: list[float], k: int) -> float:
    """Expected demand over the next ``k`` ticks (extrapolating the last mean beyond the horizon)."""
    if k <= 0:
        return 0.0
    if k <= len(means):
        return sum(means[:k])
    return sum(means) + (k - len(means)) * (means[-1] if means else 0.0)


def available_routes(world: World, station_id: str) -> list[RouteView]:
    dep = world.tick + 1
    out = []
    for r in world.routes_to(station_id):
        d = world.depots.get(r.depot_id)
        if r.status == "AVAILABLE" and not route_disrupted_at(world, r.id, dep) and d and d.status in ("OPEN", "CONSTRAINED"):
            out.append(r)
    return out


def arrivals_incl_reserved(world: World, station_id: str, fuel: str) -> list[tuple[int, float]]:
    from ..forecasts import arrivals_for

    arr = arrivals_for(world, station_id, fuel)
    for res in world.reserved:
        if res.station_id == station_id and res.fuel == fuel:
            r = world.routes.get(res.route_id)
            arr.append((world.tick + 1 + (r.transit_ticks if r else 0), res.qty))
    return arr


def build_context(world: World, forecasts: dict[tuple[str, str], PairForecast]) -> dict[tuple[str, str], PairCtx]:
    ctxs: dict[tuple[str, str], PairCtx] = {}
    for sid, st in world.stations.items():
        if st.status != "OPEN":
            continue  # would 409 STATION_CLOSED: skipped (an alert is raised by detection)
        routes = available_routes(world, sid)
        if not routes:
            continue
        for fuel in wc.FUEL_TYPES:
            pf = forecasts.get((sid, fuel))
            if pf is None or not pf.horizon:
                continue
            means = [p.mean for p in pf.horizon]
            inv, cap = st.inventory[fuel], st.capacity[fuel]
            arrivals = arrivals_incl_reserved(world, sid, fuel)
            risk = compute_risk(inv, pf.horizon, world.tick, world.tick_minutes, arrivals)
            l_min = min(1 + r.transit_ticks for r in routes)
            mean_pt = sum(means[:8]) / len(means[:8])
            arrive_by = world.tick + l_min
            proj = inv + sum(q for t, q in arrivals if t <= arrive_by) - cum_demand(means, l_min)
            safety = mean_pt * (l_min + 2)
            target = min(TARGET_FILL * cap, max(cum_demand(means, l_min + COVER_TICKS), safety))
            need = max(0.0, target - proj)
            if (risk.t_empty_ticks is not None and risk.t_empty_ticks <= l_min + 2) or risk.stockout_risk >= 0.5:
                w = 1000.0
            elif risk.stockout_risk >= 0.2:
                w = 300.0
            else:
                w = 100.0
            ctxs[(sid, fuel)] = PairCtx(
                sid, fuel, routes, inv, cap, world.station_in_flight(sid, fuel), means, risk, mean_pt, l_min,
                target, safety, proj, need, w, pf,
            )
    return ctxs


def depot_budget(world: World, depot_id: str, derate: float) -> float:
    """B(d): per-tick dispatch capacity (derated when CONSTRAINED) minus what is already spoken for this tick."""
    d = world.depots[depot_id]
    cap = d.dispatch_capacity * (derate if d.status == "CONSTRAINED" else 1.0)
    reserved = sum(r.qty for r in world.reserved if r.depot_id == depot_id)
    return max(0.0, cap - world.dispatch_used(depot_id) - reserved)


def usable_stock(world: World, depot_id: str, fuel: str) -> float:
    """U(d,f)."""
    return max(0.0, world.depots[depot_id].inventory[fuel] - world.depot_pending(depot_id, fuel))


def depot_reserve(world: World, depot_id: str, fuel: str, forecasts: dict[tuple[str, str], PairForecast]) -> float:
    """R(d,f): 0.5 x own-region forecast demand until the next supply arrival for (d,f)."""
    nxt = [s.planned_tick for s in world.supply if s.depot_id == depot_id and s.fuel == fuel
           and s.status != "ARRIVED" and s.planned_tick > world.tick]
    region = world.depots[depot_id].region_id
    total = 0.0
    for sid, st in world.stations.items():
        if st.region_id != region:
            continue
        pf = forecasts.get((sid, fuel))
        if pf and pf.horizon:
            k = (min(nxt) - world.tick) if nxt else len(pf.horizon)
            total += cum_demand([p.mean for p in pf.horizon], max(1, min(k, len(pf.horizon))))
    return 0.5 * total
