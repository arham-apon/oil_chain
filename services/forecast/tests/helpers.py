"""Synthetic demand that follows the (empirically verified) simulator formula, for unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from fsp_shared import world_constants as wc
from fsp_shared.demand import baseline_demand_at, noise_of, spike_multiplier

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def sim_time(tick: int, tick_minutes: int = 15) -> datetime:
    return T0 + timedelta(minutes=tick * tick_minutes)


def synth_series(
    station_id: str,
    fuel_type: str,
    n_ticks: int,
    *,
    tick_minutes: int = 15,
    events=(),
    seed: int = 7,
    start_tick: int = 1,
    drop_ticks=(),
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    noise = noise_of(station_id)
    rows = []
    for tick in range(start_tick, start_tick + n_ticks):
        if tick in drop_ticks:
            continue
        st = sim_time(tick, tick_minutes)
        mult = spike_multiplier(station_id, tick, list(events))
        base = baseline_demand_at(station_id, fuel_type, st.hour, tick_minutes, mult)
        rows.append((tick, st, base * (1 + rng.uniform(-noise, noise))))
    return pd.DataFrame(rows, columns=["tick", "sim_time", "demand_liters"])


ALL_PAIRS = [(s, f) for s in wc.STATIONS for f in wc.FUEL_TYPES]
