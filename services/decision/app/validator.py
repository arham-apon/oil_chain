"""Pre-flight validator: the simulator's validation order (spec §3.5 / guide §5.2) applied to the freshest state, plus
our stricter ullage rule (C5: in-flight liters count, so tanks never overflow on arrival).

``validate`` returns the first failing code or ``"OK"``. Codes match the simulator's, with two of ours added:
``ULLAGE_WITH_IN_FLIGHT`` (stricter than the simulator) and ``INVALID_QUANTITY``.
"""

from __future__ import annotations

from dataclasses import dataclass

from fsp_shared.demand import covers_tick

from .world import World

ULLAGE_FRACTION = 0.98


@dataclass(frozen=True)
class Candidate:
    depot_id: str
    station_id: str
    route_id: str
    fuel: str
    qty: float
    exclude_decision_id: str | None = None  # a decision's own reservation must not count against itself


def route_disrupted_at(world: World, route_id: str, tick: int) -> bool:
    """True if a route_disruption event (scheduled or active) covers ``tick`` for this route."""
    for e in world.events:
        if e.type == "route_disruption" and covers_tick(e, tick):
            ids = (e.parameters or {}).get("route_ids") or []
            if not ids or route_id in ids:
                return True
    return False


def validate(world: World, c: Candidate, *, derate: float = 1.0) -> str:
    if c.qty <= 0:
        return "INVALID_QUANTITY"
    depot = world.depots.get(c.depot_id)
    station = world.stations.get(c.station_id)
    route = world.routes.get(c.route_id)
    if depot is None or station is None or route is None or c.fuel not in (depot.inventory or {}):
        return "NOT_FOUND"
    if route.depot_id != c.depot_id or route.station_id != c.station_id:
        return "ROUTE_MISMATCH"
    if depot.status not in ("OPEN", "CONSTRAINED"):
        return "DEPOT_CLOSED"
    if station.status != "OPEN":
        return "STATION_CLOSED"
    if route.status != "AVAILABLE" or route_disrupted_at(world, c.route_id, world.tick + 1):
        return "ROUTE_DISRUPTED"
    if c.qty > route.max_shipment + 1e-9:
        return "ROUTE_CAPACITY_EXCEEDED"
    others = sum(
        r.qty for r in world.reserved if r.depot_id == c.depot_id and r.fuel == c.fuel and r.decision_id != c.exclude_decision_id
    )
    if depot.inventory[c.fuel] - others < c.qty - 1e-9:
        return "INSUFFICIENT_INVENTORY"
    cap = depot.dispatch_capacity * (derate if depot.status == "CONSTRAINED" else 1.0)
    if world.dispatch_used(c.depot_id) + c.qty > cap + 1e-9:
        return "DISPATCH_CAPACITY_EXCEEDED"
    if station.inventory[c.fuel] + c.qty > station.capacity[c.fuel] + 1e-9:
        return "DESTINATION_CAPACITY_EXCEEDED"
    in_flight = sum(
        a.qty for a in world.in_flight() if a.station_id == c.station_id and a.fuel == c.fuel
    ) + sum(
        r.qty for r in world.reserved
        if r.station_id == c.station_id and r.fuel == c.fuel and r.decision_id != c.exclude_decision_id
    )
    if station.inventory[c.fuel] + in_flight + c.qty > ULLAGE_FRACTION * station.capacity[c.fuel] + 1e-9:
        return "ULLAGE_WITH_IN_FLIGHT"
    return "OK"


# Codes for which shrinking the quantity once can help (spec §11.5: "if the only failure is quantity-related")
SHRINKABLE = {
    "ROUTE_CAPACITY_EXCEEDED",
    "INSUFFICIENT_INVENTORY",
    "DISPATCH_CAPACITY_EXCEEDED",
    "DESTINATION_CAPACITY_EXCEEDED",
    "ULLAGE_WITH_IN_FLIGHT",
}


def max_feasible_qty(world: World, c: Candidate, *, derate: float = 1.0) -> float:
    """Largest quantity that passes every quantity-related check (0 if none)."""
    depot, station, route = world.depots.get(c.depot_id), world.stations.get(c.station_id), world.routes.get(c.route_id)
    if depot is None or station is None or route is None:
        return 0.0
    others = sum(
        r.qty for r in world.reserved if r.depot_id == c.depot_id and r.fuel == c.fuel and r.decision_id != c.exclude_decision_id
    )
    cap = depot.dispatch_capacity * (derate if depot.status == "CONSTRAINED" else 1.0)
    in_flight = sum(a.qty for a in world.in_flight() if a.station_id == c.station_id and a.fuel == c.fuel) + sum(
        r.qty for r in world.reserved
        if r.station_id == c.station_id and r.fuel == c.fuel and r.decision_id != c.exclude_decision_id
    )
    limits = [
        route.max_shipment,
        depot.inventory[c.fuel] - others,
        cap - world.dispatch_used(c.depot_id),
        station.capacity[c.fuel] - station.inventory[c.fuel],
        ULLAGE_FRACTION * station.capacity[c.fuel] - station.inventory[c.fuel] - in_flight,
    ]
    return max(0.0, min(limits))


def shrink_and_validate(world: World, c: Candidate, *, derate: float = 1.0) -> tuple[Candidate | None, str]:
    """Validate; on a quantity-only failure shrink once to the feasible maximum (floored to 10 L) and re-validate."""
    code = validate(world, c, derate=derate)
    if code == "OK" or code not in SHRINKABLE:
        return (c if code == "OK" else None), code
    q = max_feasible_qty(world, c, derate=derate)
    q = float(int(q // 10) * 10)
    if q <= 0:
        return None, code
    shrunk = Candidate(c.depot_id, c.station_id, c.route_id, c.fuel, q, c.exclude_decision_id)
    code2 = validate(world, shrunk, derate=derate)
    return (shrunk if code2 == "OK" else None), (code2 if code2 != "OK" else "OK_SHRUNK")
