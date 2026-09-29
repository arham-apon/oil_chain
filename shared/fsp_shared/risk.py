"""Stockout risk metrics (spec §10.5), reused by decision-svc when forecast-svc is unavailable.

Step ``k`` means tick ``current_tick + k``. ``arrivals`` are ``(arrival_tick, liters)`` pairs; an arrival at tick ``a``
counts from step ``a - current_tick`` on (same-tick arrivals are treated as available for that tick's demand).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from .schemas import HorizonPoint, RiskMetrics
from .timeutil import ticks_per_hour


def normal_sf(z: float) -> float:
    """P(Z > z) for a standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def compute_risk(
    inventory: float,
    horizon: Sequence[HorizonPoint],
    current_tick: int,
    tick_minutes: int,
    arrivals: Iterable[tuple[int, float]] = (),
) -> RiskMetrics:
    if not horizon:
        return RiskMetrics(burn_rate_lph=0.0)
    pending = sorted(arrivals)
    tph = ticks_per_hour(tick_minutes)
    burn = horizon[0].mean * tph

    cum_mean = 0.0
    cum_var = 0.0
    arrived = 0.0
    ai = 0
    t_empty: int | None = None
    worst = 0.0
    min_inv = inventory
    for k, p in enumerate(horizon, start=1):
        tick = current_tick + k
        while ai < len(pending) and pending[ai][0] <= tick:
            arrived += pending[ai][1]
            ai += 1
        cum_mean += p.mean
        cum_var += p.sigma * p.sigma
        available = inventory + arrived
        projected = available - cum_mean
        min_inv = min(min_inv, projected)
        if t_empty is None and projected <= 0:
            t_empty = k
        sd = math.sqrt(cum_var)
        if sd <= 0:
            risk = 1.0 if cum_mean > available else 0.0
        else:
            risk = normal_sf((available - cum_mean) / sd)
        worst = max(worst, risk)
    return RiskMetrics(
        burn_rate_lph=burn,
        t_empty_ticks=t_empty,
        t_empty_hours=None if t_empty is None else t_empty / tph,
        stockout_risk=min(1.0, max(0.0, worst)),
        min_projected_inventory=min_inv,
    )
