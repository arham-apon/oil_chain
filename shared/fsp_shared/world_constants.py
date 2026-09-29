"""Static world data from the official simulator guide (§8). **Fallback / cold-start only.**

At runtime the live values are read over REST (capacities, routes, statuses). The demand
tables below are *not* exposed by the API, so they remain constants (used by the baseline
demand formula in the forecast service and by the local baseline fallback in decision-svc).
"""

from __future__ import annotations

from typing import Final

FUEL_TYPES: Final[tuple[str, ...]] = ("DIESEL", "PETROL", "OCTANE")

REGIONS: Final[dict[str, dict]] = {
    "region-dhaka": {"name": "Dhaka Division", "demand_factor": 1.00},
    "region-chattogram": {"name": "Chattogram Division", "demand_factor": 1.08},
}


def _fuel(d: float, p: float, o: float) -> dict[str, float]:
    return {"DIESEL": float(d), "PETROL": float(p), "OCTANE": float(o)}


DEPOTS: Final[dict[str, dict]] = {
    "depot-gazipur": {
        "region_id": "region-dhaka",
        "dispatch_capacity_per_tick": 12_000.0,
        "capacity": _fuel(90_000, 70_000, 45_000),
        "initial_inventory": _fuel(60_000, 45_000, 26_000),
    },
    "depot-patiya": {
        "region_id": "region-chattogram",
        "dispatch_capacity_per_tick": 11_000.0,
        "capacity": _fuel(85_000, 65_000, 40_000),
        "initial_inventory": _fuel(55_000, 42_000, 24_000),
    },
}

STATIONS: Final[dict[str, dict]] = {
    "station-mirpur": {
        "region_id": "region-dhaka",
        "demand_profile": "urban_high",
        "capacity": _fuel(15_000, 14_000, 9_000),
        "initial_inventory": _fuel(9_000, 9_000, 5_000),
    },
    "station-tongi": {
        "region_id": "region-dhaka",
        "demand_profile": "industrial",
        "capacity": _fuel(18_000, 9_000, 6_000),
        # NB (C1): Tongi starts with 11,000 L diesel — there is no startup emergency.
        "initial_inventory": _fuel(11_000, 6_000, 3_500),
    },
    "station-karnaphuli": {
        "region_id": "region-chattogram",
        "demand_profile": "highway",
        "capacity": _fuel(14_000, 15_000, 9_000),
        "initial_inventory": _fuel(8_500, 9_500, 5_200),
    },
    "station-coxsbazar": {
        "region_id": "region-chattogram",
        "demand_profile": "regional",
        "capacity": _fuel(12_000, 12_000, 7_000),
        "initial_inventory": _fuel(7_500, 7_500, 4_200),
    },
}

# Direct depot -> station routes only (C3). No multi-hop, no coordinates or distances (C20).
ROUTES: Final[dict[str, dict]] = {
    "route-gazipur-mirpur": {
        "depot": "depot-gazipur",
        "station": "station-mirpur",
        "transit_ticks": 2,
        "max_shipment": 7_000.0,
    },
    "route-gazipur-tongi": {
        "depot": "depot-gazipur",
        "station": "station-tongi",
        "transit_ticks": 2,
        "max_shipment": 6_500.0,
    },
    "route-patiya-karnaphuli": {
        "depot": "depot-patiya",
        "station": "station-karnaphuli",
        "transit_ticks": 2,
        "max_shipment": 7_000.0,
    },
    "route-patiya-coxsbazar": {
        "depot": "depot-patiya",
        "station": "station-coxsbazar",
        "transit_ticks": 3,
        "max_shipment": 6_000.0,
    },
    "route-gazipur-karnaphuli": {
        "depot": "depot-gazipur",
        "station": "station-karnaphuli",
        "transit_ticks": 4,
        "max_shipment": 5_000.0,
    },
    "route-patiya-mirpur": {
        "depot": "depot-patiya",
        "station": "station-mirpur",
        "transit_ticks": 4,
        "max_shipment": 5_000.0,
    },
}

# liters per simulated day, per fuel, and per-profile noise (relative std-dev)
DEMAND_PROFILES: Final[dict[str, dict]] = {
    "urban_high": {"daily": _fuel(8_500, 10_500, 5_600), "noise": 0.10},
    "industrial": {"daily": _fuel(14_000, 4_500, 2_200), "noise": 0.08},
    "highway": {"daily": _fuel(10_500, 11_000, 6_200), "noise": 0.12},
    "regional": {"daily": _fuel(7_200, 7_600, 3_600), "noise": 0.10},
}

# Hour-of-day factors (guide §8.6). ``busy`` windows are (first_hour, last_hour) and are treated
# as INCLUSIVE of the last hour, i.e. "07–09" = hours 7, 8 and 9. The guide is ambiguous for
# highway/urban_high; the forecast service confirms/overrides this empirically (spec §10.1).
HOUR_FACTORS: Final[dict[str, dict]] = {
    "industrial": {"busy": [(6, 17)], "busy_factor": 1.55, "off_factor": 0.45},
    "highway": {"busy": [(6, 9), (16, 20)], "busy_factor": 1.35, "off_factor": 0.75},
    "urban_high": {"busy": [(7, 9), (16, 20)], "busy_factor": 1.45, "off_factor": 0.70},
    "regional": {"busy": [(7, 20)], "busy_factor": 1.25, "off_factor": 0.65},
}

SUPPLY_ARRIVAL_COUNT: Final[int] = 22
SUPPLY_RECURRING_INTERVAL_TICKS: Final[int] = 64

# Stations reachable through exactly one route — no alternate path exists (C2).
SINGLE_ROUTE_STATIONS: Final[frozenset[str]] = frozenset(
    s for s in STATIONS if sum(1 for r in ROUTES.values() if r["station"] == s) == 1
)


def routes_to_station(station_id: str) -> list[str]:
    return [rid for rid, r in ROUTES.items() if r["station"] == station_id]


def hour_factor(profile: str, hour: int) -> float:
    cfg = HOUR_FACTORS[profile]
    for lo, hi in cfg["busy"]:
        if lo <= hour <= hi:
            return float(cfg["busy_factor"])
    return float(cfg["off_factor"])


def baseline_demand(
    profile: str,
    fuel_type: str,
    hour: int,
    tick_minutes: int,
    region_factor: float = 1.0,
    demand_multiplier: float = 1.0,
) -> float:
    """Expected demand (liters) for one tick — spec §10.1 baseline formula."""
    daily = DEMAND_PROFILES[profile]["daily"][fuel_type]
    return daily * (tick_minutes / 1440.0) * hour_factor(profile, hour) * region_factor * demand_multiplier
