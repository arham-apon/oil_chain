"""Detection (spec §11.8) -> alert candidates. Pure function of (world, forecasts, contexts); the loop persists them
(de-duplicated per kind/entity/fuel over a window of ticks)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fsp_shared import world_constants as wc
from fsp_shared.schemas import PairForecast

from .planner.context import PairCtx, available_routes
from .validator import route_disrupted_at
from .world import World


@dataclass
class AlertCandidate:
    kind: str
    severity: str
    message: str
    entity_id: str | None = None
    fuel: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class AnomalyTracker:
    """Demand anomaly: (observed - forecast)/sigma > 3 for 2 consecutive ticks."""

    def __init__(self) -> None:
        # per pair: tick -> (mean, sigma) as forecast *before* that tick was observed
        self.prev_forecast: dict[tuple[str, str], dict[int, tuple[float, float]]] = {}
        self.last_checked: dict[tuple[str, str], int] = {}
        self.streak: dict[tuple[str, str], int] = {}

    def update(self, observed: dict[tuple[str, str], tuple[int, float]], forecasts: dict[tuple[str, str], PairForecast]
               ) -> list[AlertCandidate]:
        out = []
        for key, (tick, value) in observed.items():
            fc = self.prev_forecast.get(key, {}).get(tick)
            if fc is None or self.last_checked.get(key) == tick:
                continue
            self.last_checked[key] = tick
            z = (value - fc[0]) / max(fc[1], 1e-6)
            self.streak[key] = self.streak.get(key, 0) + 1 if z > 3 else 0
            if self.streak[key] == 2:
                out.append(AlertCandidate("DEMAND_ANOMALY", "MEDIUM",
                                          f"Demand at {key[0]} {key[1]} is >3 sigma above forecast on consecutive checks",
                                          key[0], key[1], {"z": round(z, 2), "observed": value, "forecast": fc[0]}))
        for key, pf in forecasts.items():
            book = self.prev_forecast.setdefault(key, {})
            for p in pf.horizon:
                book.setdefault(p.tick, (p.mean, p.sigma))  # keep the earliest (true out-of-sample) forecast
            if len(book) > 200:
                for t in sorted(book)[:-100]:
                    del book[t]
        return out


def detect(world: World, forecasts: dict[tuple[str, str], PairForecast], ctxs: dict[tuple[str, str], PairCtx]
           ) -> list[AlertCandidate]:
    out: list[AlertCandidate] = []
    l_max = max((1 + r.transit_ticks for r in world.routes.values()), default=5)

    # outage / closed stations
    for sid, st in world.stations.items():
        if st.status != "OPEN":
            out.append(AlertCandidate("STATION_OUTAGE", "HIGH", f"{sid} is in OUTAGE: no dispatch possible", sid))

    # bottlenecks: route disrupted now or scheduled within L_max
    for rid, r in world.routes.items():
        now = r.status != "AVAILABLE"
        soon = any(route_disrupted_at(world, rid, world.tick + k) for k in range(1, l_max + 1))
        if not (now or soon):
            continue
        alternates = [x.id for x in available_routes(world, r.station_id) if x.id != rid]
        single = r.station_id in wc.SINGLE_ROUTE_STATIONS or not alternates
        msg = f"Route {rid} {'is DISRUPTED' if now else 'will be disrupted within ' + str(l_max) + ' ticks'}"
        msg += "; NO ALTERNATE PATH to " + r.station_id if single else f"; reroute via {', '.join(alternates)}"
        out.append(AlertCandidate("BOTTLENECK", "HIGH" if single else "MEDIUM", msg, rid,
                                  data={"station_id": r.station_id, "alternates": alternates, "active": now}))

    # stock levels and imminent stockouts
    region_risky: dict[str, int] = {}
    for (sid, fuel), c in ctxs.items():
        if c.risk.t_empty_ticks is not None and c.risk.t_empty_ticks < c.l_min + 2:
            out.append(AlertCandidate("STOCKOUT_IMMINENT", "CRITICAL",
                                      f"{sid} {fuel} projected empty in {c.risk.t_empty_ticks} ticks", sid, fuel,
                                      {"t_empty_ticks": c.risk.t_empty_ticks, "stockout_risk": c.risk.stockout_risk}))
        elif c.inventory < c.safety_stock:
            out.append(AlertCandidate("LOW_INVENTORY", "MEDIUM", f"{sid} {fuel} below safety stock", sid, fuel,
                                      {"inventory": c.inventory, "safety_stock": c.safety_stock}))
        if c.risk.stockout_risk >= 0.5:
            region_risky[world.stations[sid].region_id] = region_risky.get(world.stations[sid].region_id, 0) + 1
    for region, n in region_risky.items():
        if n >= 2:
            out.append(AlertCandidate("REGIONAL_DISRUPTION", "HIGH", f"{n} station/fuel pairs in {region} at >=50% risk",
                                      region))

    # supply delays / shortfalls
    for s in world.supply:
        if s.status == "DELAYED" or s.planned_tick > s.first_planned_tick:
            out.append(AlertCandidate("SUPPLY_DELAYED", "MEDIUM",
                                      f"Supply {s.id} to {s.depot_id} {s.fuel} delayed to tick {s.planned_tick}",
                                      s.id, s.fuel, {"planned_tick": s.planned_tick, "first": s.first_planned_tick}))
        if s.qty < s.first_qty - 1e-6:
            out.append(AlertCandidate("SUPPLY_SHORTFALL", "HIGH",
                                      f"Supply {s.id} reduced {s.first_qty:.0f} -> {s.qty:.0f} L", s.id, s.fuel,
                                      {"quantity": s.qty, "first_quantity": s.first_qty}))
    return out
