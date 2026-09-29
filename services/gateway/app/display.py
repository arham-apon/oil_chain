"""Display names for the fixed world entities. The simulator's snapshot tables do not carry names, and the world is
fixed (spec §3.3), so names live here. Map positions are a frontend concern (``frontend/src/config/network.ts``)."""

from __future__ import annotations

from fsp_shared import world_constants as wc

NAMES: dict[str, str] = {
    "depot-gazipur": "Gazipur Depot",
    "depot-patiya": "Patiya Depot",
    "station-mirpur": "Mirpur",
    "station-tongi": "Tongi",
    "station-karnaphuli": "Karnaphuli",
    "station-coxsbazar": "Cox's Bazar",
    "region-dhaka": "Dhaka",
    "region-chattogram": "Chattogram",
}


def name(entity_id: str | None) -> str | None:
    return None if entity_id is None else NAMES.get(entity_id, entity_id)


def station_meta(station_id: str) -> dict:
    s = wc.STATIONS.get(station_id, {})
    routes = wc.routes_to_station(station_id)
    return {
        "name": name(station_id),
        "region_id": s.get("region_id"),
        "demand_profile": s.get("demand_profile"),
        "routes": routes,
        "single_route": station_id in wc.SINGLE_ROUTE_STATIONS,
    }
