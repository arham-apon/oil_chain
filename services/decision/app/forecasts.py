"""Forecast access for the decision loop: forecast-svc over HTTP (behind the ``forecast`` circuit breaker, 500 ms
timeout) with a local baseline fallback that needs no other service (spec §11.1 step 4)."""

from __future__ import annotations

import httpx
from fsp_shared import world_constants as wc
from fsp_shared.breaker import CircuitBreaker
from fsp_shared.demand import baseline_horizon
from fsp_shared.logging import get_logger
from fsp_shared.risk import compute_risk
from fsp_shared.schemas import PairForecast

from .world import World

log = get_logger("decision.forecasts")
Pairs = dict[tuple[str, str], PairForecast]


def arrivals_for(world: World, station_id: str, fuel: str) -> list[tuple[int, float]]:
    """(arrival_tick, liters) of sim allocations still on their way (arrivals at or before ``now`` are already in stock)."""
    out = []
    for a in world.in_flight():
        if a.station_id != station_id or a.fuel != fuel:
            continue
        if a.expected_arrival_tick is not None:
            t = a.expected_arrival_tick
        else:
            r = world.routes.get(a.route_id)
            t = (a.created_tick or world.tick) + 1 + (r.transit_ticks if r else 0)
        if t > world.tick:
            out.append((t, a.qty))
    return out


def local_forecasts(world: World, horizon: int) -> Pairs:
    """Baseline forecasts + risk computed locally (source BASELINE_FALLBACK)."""
    out: Pairs = {}
    for sid, st in world.stations.items():
        for fuel in wc.FUEL_TYPES:
            pts = baseline_horizon(sid, fuel, world.tick, world.sim_time, world.tick_minutes, horizon, world.events)
            inv = st.inventory.get(fuel, 0.0)
            arr = arrivals_for(world, sid, fuel)
            risk = compute_risk(inv, pts, world.tick, world.tick_minutes, arr)
            out[(sid, fuel)] = PairForecast(
                station_id=sid, fuel_type=fuel, tick=world.tick, horizon=pts,  # type: ignore[arg-type]
                burn_rate_lph=risk.burn_rate_lph, t_empty_ticks=risk.t_empty_ticks, t_empty_hours=risk.t_empty_hours,
                stockout_risk=risk.stockout_risk, residual_sigma=sum(p.sigma for p in pts) / len(pts),
                model_version=None, source="BASELINE_FALLBACK", current_inventory=inv,
                inflight_liters=sum(q for _, q in arr), data_stale=world.stale,
            )
    return out


class ForecastClient:
    def __init__(self, base_url: str, breaker: CircuitBreaker, timeout_s: float = 0.5,
                 http: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.breaker = breaker
        self.timeout_s = timeout_s
        self._http = http or httpx.AsyncClient(timeout=timeout_s)

    async def fetch(self, horizon: int) -> Pairs:
        async def call() -> Pairs:
            r = await self._http.get(f"{self.base_url}/forecast", params={"horizon": horizon})
            r.raise_for_status()
            return {(f["station_id"], f["fuel_type"]): PairForecast.model_validate(f) for f in r.json()["forecasts"]}

        return await self.breaker.call(call, timeout=self.timeout_s)

    async def get(self, world: World, horizon: int) -> tuple[Pairs, str]:
        """(forecasts, source) where source is MODEL | BASELINE_FALLBACK. Never raises."""
        try:
            pairs = await self.fetch(horizon + 3)
            # forecast-svc may trail ingestion by a tick or two: tolerate that (trim the stale head), otherwise distrust it
            lag = world.tick - next(iter(pairs.values())).tick if pairs else 0
            if not pairs or not 0 <= lag <= 2:
                raise RuntimeError(f"forecast tick mismatch (lag {lag})")
            for pf in pairs.values():
                pf.horizon = [p for p in pf.horizon if p.tick > world.tick][:horizon]
            source = "MODEL" if any(p.source == "MODEL" for p in pairs.values()) else "BASELINE_FALLBACK"
            return pairs, source
        except Exception as exc:  # noqa: BLE001 - breaker-open, timeout, HTTP error, mismatch -> local baseline
            self.breaker.record_fallback(type(exc).__name__)
            log.warning("forecast_fallback", reason=f"{type(exc).__name__}: {str(exc)[:100]}")
            return local_forecasts(world, horizon), "BASELINE_FALLBACK"

    async def aclose(self) -> None:
        await self._http.aclose()
