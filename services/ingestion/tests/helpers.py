"""Builders for fake simulator payloads + a respx router that serves them."""

from __future__ import annotations

from typing import Any

import httpx
import respx

BASE = "http://sim.test"
FUELS = ("DIESEL", "PETROL", "OCTANE")
STATION_IDS = ("station-mirpur", "station-tongi", "station-karnaphuli", "station-coxsbazar")


def fuel(d: float, p: float, o: float) -> dict[str, float]:
    return {"DIESEL": d, "PETROL": p, "OCTANE": o}


def make_world(tick: int = 5, *, status: str = "RUNNING", station_multiplier: float = 1.0) -> dict[str, Any]:
    return {
        "instance": {
            "id": 1,
            "scenario_id": "baseline",
            "scenario_version": "1.0",
            "seed": 12345,
            "sim_time": f"2026-01-01T{(tick * 15 // 60) % 24:02d}:{(tick * 15) % 60:02d}:00",
            "tick": tick,
            "tick_minutes": 15,
            "status": status,
        },
        "depots": [
            {
                "id": "depot-gazipur",
                "name": "G",
                "region_id": "region-dhaka",
                "status": "OPEN",
                "dispatch_capacity_per_tick": 12000,
                "capacity": fuel(90000, 70000, 45000),
                "inventory": fuel(60000 - tick, 45000, 26000),
            },
            {
                "id": "depot-patiya",
                "name": "P",
                "region_id": "region-chattogram",
                "status": "OPEN",
                "dispatch_capacity_per_tick": 11000,
                "capacity": fuel(85000, 65000, 40000),
                "inventory": fuel(55000, 42000, 24000),
            },
        ],
        "stations": [
            {
                "id": sid,
                "name": sid,
                "region_id": "region-dhaka",
                "status": "OPEN",
                "demand_profile": "urban_high",
                "demand_multiplier": station_multiplier,
                "capacity": fuel(15000, 14000, 9000),
                "inventory": fuel(9000 - tick, 9000, 5000),
            }
            for sid in STATION_IDS
        ],
        "routes": [
            {
                "id": "route-gazipur-mirpur",
                "source_depot_id": "depot-gazipur",
                "destination_station_id": "station-mirpur",
                "transit_ticks": 2,
                "max_shipment": 7000,
                "status": "AVAILABLE",
            },
            {
                "id": "route-gazipur-tongi",
                "source_depot_id": "depot-gazipur",
                "destination_station_id": "station-tongi",
                "transit_ticks": 2,
                "max_shipment": 6500,
                "status": "AVAILABLE",
            },
        ],
        "events": [],
        "allocations": [],
        "metrics": {
            "served_demand_liters": 100.0 * tick,
            "unmet_demand_liters": 0.0,
            "service_level": 1.0,
            "allocation_liters": 0.0,
            "allocation_failures": 0,
        },
        "supply": [
            {
                "id": "supply-001",
                "depot_id": "depot-gazipur",
                "fuel_type": "DIESEL",
                "quantity": 18000,
                "planned_tick": 12,
                "actual_tick": None,
                "status": "SCHEDULED",
            },
        ],
        "demand": {},
    }


def demand_rows(station_id: str, ticks: range, start_id: int = 1) -> list[dict[str, Any]]:
    rows, i = [], start_id
    for t in ticks:
        for f in FUELS:
            rows.append(
                {
                    "id": i,
                    "station_id": station_id,
                    "fuel_type": f,
                    "tick": t,
                    "sim_time": f"2026-01-01T{(t * 15 // 60) % 24:02d}:{(t * 15) % 60:02d}:00",
                    "demand_liters": 100.0 + t,
                    "served_liters": 100.0 + t,
                    "unmet_liters": 0.0,
                }
            )
            i += 1
    return rows


def allocation(alloc_id: int, status: str = "PENDING", **kw: Any) -> dict[str, Any]:
    base = {
        "id": alloc_id,
        "idempotency_key": f"fsp-{alloc_id}",
        "source_depot_id": "depot-gazipur",
        "destination_station_id": "station-mirpur",
        "route_id": "route-gazipur-mirpur",
        "fuel_type": "DIESEL",
        "quantity": 3000,
        "created_tick": 5,
        "departure_tick": None,
        "expected_arrival_tick": None,
        "actual_arrival_tick": None,
        "status": status,
        "failure_reason": None,
    }
    return {**base, **kw}


PATHS = {
    "instance": "/v1/instance",
    "depots": "/v1/depots",
    "stations": "/v1/stations",
    "routes": "/v1/routes",
    "events": "/v1/events",
    "allocations": "/v1/allocations",
    "metrics": "/v1/metrics",
    "supply": "/v1/supply-arrivals",
}


class FakeSim:
    """Mutable fake simulator. Change ``world`` / ``headers`` / ``overrides`` between syncs."""

    def __init__(self, router: respx.MockRouter, world: dict[str, Any] | None = None) -> None:
        self.world = world or make_world()
        self.headers: dict[str, str] = {}
        self.overrides: dict[str, httpx.Response] = {}  # endpoint key -> forced response
        self.demand_requests: list[dict[str, str]] = []
        for key, path in PATHS.items():
            router.get(f"{BASE}{path}").mock(side_effect=self._handler(key))
        router.get(f"{BASE}/v1/demand-history").mock(side_effect=self._demand)

    def _handler(self, key: str):
        def handler(request: httpx.Request) -> httpx.Response:
            if key in self.overrides:
                return self.overrides[key]
            return httpx.Response(200, json=self.world[key], headers=self.headers)

        return handler

    def _demand(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.demand_requests.append(params)
        if "demand" in self.overrides:
            return self.overrides["demand"]
        rows = self.world["demand"].get(params.get("station_id"), [])
        limit = int(params["limit"])
        return httpx.Response(200, json=sorted(rows, key=lambda r: -r["id"])[:limit], headers=self.headers)
