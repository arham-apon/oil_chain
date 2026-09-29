"""Copilot tools (spec §12.4). All read-only except ``propose_allocation``, which only creates a STAGED_REVIEW
decision (an operator must approve it in the UI). Nothing here can POST to the simulator."""

from __future__ import annotations

from typing import Any

import httpx
from fsp_shared import world_constants as wc
from langchain_core.tools import StructuredTool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class ToolBox:
    def __init__(self, engine: AsyncEngine, decision_url: str, forecast_url: str) -> None:
        self.engine = engine
        self.decision_url = decision_url.rstrip("/")
        self.forecast_url = forecast_url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=8.0)

    async def _rows(self, sql: str, **p: Any) -> list[dict]:
        async with self.engine.connect() as c:
            return [dict(r) for r in (await c.execute(text(sql), p)).mappings().all()]

    # --- tool implementations ---------------------------------------------------------------------------------------
    async def get_network_state(self) -> dict:
        """Current tick, KPIs, station and depot inventories, route statuses and active/scheduled events."""
        tick = await self._rows("SELECT tick, sim_time, status, stale FROM sim_ticks ORDER BY tick DESC LIMIT 1")
        metrics = await self._rows("SELECT * FROM sim_metrics ORDER BY tick DESC LIMIT 1")
        return {"instance": tick[0] if tick else None, "metrics": metrics[0] if metrics else None,
                "stations": [await self.get_station(s) for s in wc.STATIONS],
                "depots": [await self.get_depot(d) for d in wc.DEPOTS], "routes": await self.get_routes(),
                "events": await self.get_events("SCHEDULED,ACTIVE"), "data_is_simulated": True}

    async def get_station(self, station_id: str) -> dict:
        """Latest inventory, capacity, status and demand multiplier per fuel for one station."""
        rows = await self._rows(
            "SELECT DISTINCT ON (fuel_type) fuel_type, inventory, capacity, demand_multiplier, status, tick "
            "FROM station_snapshots WHERE station_id = :s ORDER BY fuel_type, tick DESC", s=station_id)
        return {"station_id": station_id, "routes": wc.routes_to_station(station_id),
                "single_route": station_id in wc.SINGLE_ROUTE_STATIONS, "fuels": rows}

    async def get_depot(self, depot_id: str) -> dict:
        """Latest inventory, capacity, dispatch capacity and status per fuel for one depot."""
        rows = await self._rows(
            "SELECT DISTINCT ON (fuel_type) fuel_type, inventory, capacity, dispatch_capacity_per_tick, status, tick "
            "FROM depot_snapshots WHERE depot_id = :d ORDER BY fuel_type, tick DESC", d=depot_id)
        return {"depot_id": depot_id, "fuels": rows}

    async def get_routes(self) -> list[dict]:
        """All direct depot->station routes with status, transit ticks and max shipment."""
        rows = await self._rows("SELECT DISTINCT ON (route_id) route_id, status, transit_ticks, max_shipment "
                                "FROM route_snapshots ORDER BY route_id, tick DESC")
        for r in rows:
            r.update({"depot": wc.ROUTES[r["route_id"]]["depot"], "station": wc.ROUTES[r["route_id"]]["station"]})
        return rows

    async def get_events(self, status: str | None = None) -> list[dict]:
        """Simulator events, optionally filtered by comma-separated status (SCHEDULED, ACTIVE, RESOLVED)."""
        if status:
            return await self._rows("SELECT id, type, start_tick, end_tick, status, parameters FROM sim_events "
                                    "WHERE status = ANY(:st) ORDER BY id DESC LIMIT 50", st=status.split(","))
        return await self._rows("SELECT id, type, start_tick, end_tick, status, parameters FROM sim_events "
                                "ORDER BY id DESC LIMIT 50")

    async def get_supply_arrivals(self) -> list[dict]:
        """Upcoming and recent depot supply arrivals (planned tick, quantity, status)."""
        return await self._rows("SELECT id, depot_id, fuel_type, quantity, first_quantity, planned_tick, actual_tick, "
                                "status FROM supply_arrivals ORDER BY planned_tick LIMIT 40")

    async def get_forecast(self, station_id: str, fuel_type: str) -> dict:
        """Demand forecast, burn rate, time-to-empty and stockout risk for one station and fuel (DIESEL/PETROL/OCTANE)."""
        fuel_type = fuel_type.upper()
        r = await self.http.get(f"{self.forecast_url}/forecast", params={"station_id": station_id,
                                                                         "fuel_type": fuel_type, "horizon": 24})
        r.raise_for_status()
        f = r.json()["forecasts"][0]
        h = f.pop("horizon")
        f["next_ticks_mean_liters"] = [round(p["mean"], 1) for p in h[:8]]
        f["horizon_24_sum_liters"] = round(sum(p["mean"] for p in h), 1)
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in f.items()}

    async def get_decisions(self, status: str | None = None) -> list[dict]:
        """Recent platform decisions (optionally filtered by status, e.g. STAGED_REVIEW)."""
        r = await self.http.get(f"{self.decision_url}/decisions", params={"status": status, "limit": 20} if status
                                else {"limit": 20})
        r.raise_for_status()
        return [{k: d.get(k) for k in ("decision_id", "status", "station_id", "fuel_type", "route_id", "quantity",
                                       "system_origin", "cycle_tick")} for d in r.json()]

    async def simulate_allocation(self, station_id: str, fuel_type: str, qty: float, route_id: str,
                                  depot_derate: dict[str, float] | None = None,
                                  route_disabled: list[str] | None = None,
                                  demand_multiplier: dict[str, float] | None = None) -> dict:
        """What-if: project stockout risk before/after dispatching qty liters on route_id, optionally under overrides
        (depot_derate: depot_id -> factor on dispatch capacity, route_disabled: route ids, demand_multiplier:
        station_id -> multiplier). Pure simulation; never dispatches. Use qty=0.0001 to evaluate overrides only."""
        fuel_type = fuel_type.upper()
        body = {"proposals": [{"station_id": station_id, "fuel_type": fuel_type, "qty": qty, "route_id": route_id}],
                "overrides": {"depot_derate": depot_derate or {}, "route_disabled": route_disabled or [],
                              "demand_multiplier": demand_multiplier or {}}}
        r = await self.http.post(f"{self.decision_url}/whatif", json=body)
        r.raise_for_status()
        out = r.json()
        for p in out.get("pairs", []):
            p.pop("curve", None)  # the model only needs the risk / time-to-empty numbers
        return out

    async def propose_allocation(self, station_id: str, fuel_type: str, qty: float, route_id: str,
                                 rationale: str = "") -> dict:
        """Create a STAGED_REVIEW proposal for the operator to approve in the Decision Center (never dispatches)."""
        fuel_type = fuel_type.upper()
        r = await self.http.post(f"{self.decision_url}/decisions/manual", json={
            "station_id": station_id, "fuel_type": fuel_type, "qty": qty, "route_id": route_id,
            "operator": "copilot", "note": rationale[:300], "staged": True, "origin": "SYSTEM2_OVERRIDE"})
        if r.status_code >= 400:
            return {"created": False, "error": r.text[:300]}
        d = r.json()
        return {"created": True, "decision_id": d["decision_id"], "status": d["status"]}

    def langchain_tools(self) -> list[StructuredTool]:
        fns = [self.get_network_state, self.get_station, self.get_depot, self.get_routes, self.get_events,
               self.get_supply_arrivals, self.get_forecast, self.get_decisions, self.simulate_allocation,
               self.propose_allocation]
        return [StructuredTool.from_function(coroutine=f, name=f.__name__, description=f.__doc__ or f.__name__)
                for f in fns]

    async def aclose(self) -> None:
        await self.http.aclose()
