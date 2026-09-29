"""Turns the in-memory world + the active model bundle into per-pair forecasts and risk metrics (spec §10.4-10.6)."""

from __future__ import annotations

import time
from collections.abc import Sequence

import numpy as np
from fsp_shared import world_constants as wc
from fsp_shared.demand import baseline_horizon, spike_multiplier
from fsp_shared.risk import compute_risk
from fsp_shared.schemas import PairForecast
from fsp_shared.timeutil import project_sim_time
from pydantic import BaseModel

from . import features as F
from . import metrics as m
from .model import MIN_TRAIN_ROWS
from .state import PairState, StateCache, WorldState
from .trainer import ModelHolder

MAX_HORIZON = 96


class ForecastResponse(BaseModel):
    tick: int
    sim_time: str
    tick_minutes: int
    model_version: str | None
    forecasts: list[PairForecast]


class NoStateError(Exception):
    """Nothing has been ingested yet."""


class Forecaster:
    def __init__(self, cache: StateCache, holder: ModelHolder, default_horizon: int = 24) -> None:
        self.cache = cache
        self.holder = holder
        self.default_horizon = default_horizon
        self._memo: dict[tuple, bytes] = {}
        self._memo_key: tuple[int, str | None] | None = None
        holder.on_change = self.invalidate

    def invalidate(self) -> None:
        self._memo.clear()

    @staticmethod
    def _lag_inputs(world: WorldState, ps: PairState) -> tuple[np.ndarray, int]:
        """Recent demand divided by the spike multiplier that applied at each tick (D18), restricted to ticks the
        snapshot already covers, plus the tick of the newest observation (it can trail the snapshot tick because
        demand history is synced more slowly than the snapshots)."""
        if len(ps.tail_ticks) != len(ps.demand_tail):
            return ps.demand_tail, world.tick
        keep = ps.tail_ticks <= world.tick
        ticks, values = ps.tail_ticks[keep], ps.demand_tail[keep]
        if len(values) == 0:
            return values, world.tick
        mults = np.array([spike_multiplier(ps.station_id, int(t), world.events) for t in ticks])
        return values / mults, int(ticks[-1])

    # ------------------------------------------------------------------------------------------
    def _pair(self, world: WorldState, ps: PairState, horizon: int) -> PairForecast:
        bundle = self.holder.bundle
        model = bundle.get(ps.station_id, ps.fuel_type) if bundle else None
        norm_tail, tail_last = self._lag_inputs(world, ps)
        usable = model is not None and ps.n_rows >= MIN_TRAIN_ROWS and len(norm_tail) >= F.MIN_HISTORY
        if usable:
            assert model is not None
            lag = F.LagState.from_series(norm_tail)
            # Recurse from the newest observed tick and drop the steps up to "now", so the horizon always starts at
            # world.tick + 1 even when demand history trails the snapshot by a few ticks.
            gap = max(0, world.tick - tail_last)
            anchor_time = project_sim_time(world.sim_time, world.tick, tail_last, world.tick_minutes)
            points = model.forecast(lag, tail_last, anchor_time, world.tick_minutes, horizon + gap, world.events)[gap:]
            source, sigma, version = "MODEL", model.residual_sigma, bundle.version if bundle else None
        else:
            points = baseline_horizon(
                ps.station_id, ps.fuel_type, world.tick, world.sim_time, world.tick_minutes, horizon, world.events
            )
            source, version = "BASELINE_FALLBACK", None
            sigma = sum(p.sigma for p in points) / len(points)
        m.FORECAST_SOURCE.labels(source).inc()

        # arrivals already reflected in the snapshot inventory (tick <= now) must not be counted twice
        flights = [a for a in world.in_flight_for(ps.station_id, ps.fuel_type) if a.arrival_tick > world.tick]
        inflight = sum(a.quantity for a in flights)
        if ps.inventory is None:
            burn = points[0].mean * (60.0 / world.tick_minutes)
            return PairForecast(
                station_id=ps.station_id, fuel_type=ps.fuel_type, tick=world.tick, horizon=points,  # type: ignore[arg-type]
                burn_rate_lph=burn, residual_sigma=sigma, model_version=version, source=source,  # type: ignore[arg-type]
                inflight_liters=inflight, data_stale=ps.stale,
            )
        risk = compute_risk(
            ps.inventory, points, world.tick, world.tick_minutes, [(a.arrival_tick, a.quantity) for a in flights]
        )
        return PairForecast(
            station_id=ps.station_id,
            fuel_type=ps.fuel_type,  # type: ignore[arg-type]
            tick=world.tick,
            horizon=points,
            burn_rate_lph=risk.burn_rate_lph,
            t_empty_ticks=risk.t_empty_ticks,
            t_empty_hours=risk.t_empty_hours,
            stockout_risk=risk.stockout_risk,
            residual_sigma=sigma,
            model_version=version,
            source=source,  # type: ignore[arg-type]
            current_inventory=ps.inventory,
            inflight_liters=inflight,
            data_stale=ps.stale,
        )

    def forecast_json(
        self,
        station_ids: Sequence[str] | None = None,
        fuel_types: Sequence[str] | None = None,
        horizon: int | None = None,
    ) -> tuple[bytes, WorldState]:
        """Serialised :class:`ForecastResponse`, memoised per (world snapshot, model, selection, horizon)."""
        world = self.cache.world
        if world is None:
            raise NoStateError("no simulator state ingested yet")
        horizon = min(MAX_HORIZON, max(1, horizon or self.default_horizon))
        stations = tuple(station_ids) if station_ids else tuple(wc.STATIONS)
        fuels = tuple(fuel_types) if fuel_types else tuple(wc.FUEL_TYPES)
        bundle = self.holder.bundle
        key = (world.version, bundle.version if bundle else None, stations, fuels, horizon)
        hit = self._memo.get(key)
        if hit is not None:
            m.FORECAST_CACHE_HITS.inc()
            return hit, world
        if self._memo_key != key[:2]:  # new snapshot or model: drop old entries
            self._memo.clear()
            self._memo_key = key[:2]
        started = time.perf_counter()
        forecasts = [
            self._pair(world, world.pairs[(s, f)], horizon) for s in stations for f in fuels if (s, f) in world.pairs
        ]
        body = ForecastResponse(
            tick=world.tick,
            sim_time=world.sim_time.isoformat(),
            tick_minutes=world.tick_minutes,
            model_version=bundle.version if bundle else None,
            forecasts=forecasts,
        ).model_dump_json().encode()
        m.FORECAST_INFERENCE.observe(time.perf_counter() - started)
        self._memo[key] = body
        return body, world
