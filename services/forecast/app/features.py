"""Feature engineering (spec §10.3). One definition is used for training (vectorised) and inference (step by step),
and ``tests/test_features.py`` checks that both agree and that no feature leaks the target.

Features for station ``s``, fuel ``f``, tick ``t`` (``d`` = observed demand):

===================  ===========================================================================
sin_hour, cos_hour   ``theta = 2*pi*hour(t)/24`` (hour taken from the row's own ``sim_time``)
ewma_4, ewma_16      EWMA (span 4 / 16, ``alpha = 2/(span+1)``) of ``d`` up to ``t-1`` (shifted by 1)
momentum_4           ``d(t-1) - d(t-5)``
demand_multiplier    product of demand_spike multipliers covering ``(s, t)`` (events; end tick inclusive)
event_active         1 if any demand_spike covers ``(s, t)``
baseline             spec §10.1 formula WITHOUT the multiplier (see below)
hour_factor          profile hour-of-day factor
===================  ===========================================================================

**Multiplier handling (DECISIONS D18).** The simulator multiplies demand by ``demand_multiplier`` exactly, so the
model works on *multiplier-normalised* demand: the regression target is ``y / mult``, the lag features (EWMAs,
momentum) are computed on ``d / mult`` and the served forecast is ``mult_future x model(...)``. A model trained on
spike-free history therefore scales correctly the moment a spike is scheduled/active, instead of having to
extrapolate a feature it never saw vary. ``demand_multiplier`` / ``event_active`` stay in the feature set (spec §10.3).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

import numpy as np
import pandas as pd
from fsp_shared import world_constants as wc
from fsp_shared.demand import (
    EventLike,
    baseline_demand_at,
    profile_of,
    region_factor,
    spike_active,
    spike_multiplier,
    station_outage,
)

FEATURES: list[str] = [
    "sin_hour",
    "cos_hour",
    "ewma_4",
    "ewma_16",
    "momentum_4",
    "demand_multiplier",
    "event_active",
    "baseline",
    "hour_factor",
]
BASELINE_IDX = FEATURES.index("baseline")  # the ridge learns a residual on top of this feature (D19)
ALPHA_4 = 2.0 / (4 + 1)
ALPHA_16 = 2.0 / (16 + 1)
WARMUP_ROWS = 16  # the EWMA-16 needs a few spans before it stops depending on its seed
MIN_HISTORY = 5  # d(t-5) is the oldest lag


def hour_angle(hour: int) -> tuple[float, float]:
    theta = 2.0 * math.pi * hour / 24.0
    return math.sin(theta), math.cos(theta)


def feature_row(
    station_id: str,
    fuel_type: str,
    hour: int,
    tick_minutes: int,
    multiplier: float,
    event_active: bool,
    ewma4: float,
    ewma16: float,
    d_prev1: float,
    d_prev5: float,
) -> np.ndarray:
    """Feature vector for a single (future) tick (inference path). ``ewma*`` / ``d_prev*`` are multiplier-normalised."""
    s, c = hour_angle(hour)
    hf = wc.hour_factor(profile_of(station_id), hour)
    baseline = baseline_demand_at(station_id, fuel_type, hour, tick_minutes, 1.0)  # multiplier applied structurally
    return np.array(
        [s, c, ewma4, ewma16, d_prev1 - d_prev5, multiplier, 1.0 if event_active else 0.0, baseline, hf],
        dtype=float,
    )


class LagState:
    """Rolling state needed to step the forecast forward: EWMAs and the last five demands."""

    __slots__ = ("e4", "e16", "last5")

    def __init__(self, e4: float, e16: float, last5: list[float]) -> None:
        self.e4, self.e16, self.last5 = e4, e16, last5  # last5[-1] = d(t-1)

    @classmethod
    def from_series(cls, demands: Sequence[float]) -> LagState:
        if len(demands) < MIN_HISTORY:
            raise ValueError(f"need at least {MIN_HISTORY} observations, got {len(demands)}")
        e4 = e16 = float(demands[0])
        for x in demands[1:]:
            e4 = ALPHA_4 * x + (1 - ALPHA_4) * e4
            e16 = ALPHA_16 * x + (1 - ALPHA_16) * e16
        return cls(e4, e16, [float(v) for v in demands[-MIN_HISTORY:]])

    def d_prev1(self) -> float:
        return self.last5[-1]

    def d_prev5(self) -> float:
        return self.last5[-5]

    def push(self, d: float) -> None:
        self.e4 = ALPHA_4 * d + (1 - ALPHA_4) * self.e4
        self.e16 = ALPHA_16 * d + (1 - ALPHA_16) * self.e16
        self.last5 = [*self.last5[1:], d]


# --------------------------------------------------------------------------------------------------
# training frames
# --------------------------------------------------------------------------------------------------
def build_training_frame(
    station_id: str,
    fuel_type: str,
    observations: pd.DataFrame,
    tick_minutes: int,
    events: Iterable[EventLike] = (),
    outage_ticks: Iterable[int] = (),
) -> pd.DataFrame:
    """``observations``: columns ``tick, sim_time, demand_liters`` for one (station, fuel).

    Returns one row per usable tick with ``FEATURES``, the raw target ``y`` (= demand_liters), the normalised
    regression target ``y_norm`` (= y / demand_multiplier), ``tick``, the naive predictors ``naive_last`` / ``naive_formula`` and ``outage`` (rows with the station in OUTAGE are flagged so
    callers can exclude them). Ticks missing from the history are forward-filled for the lag features only and
    never used as targets.
    """
    events = list(events)
    outage = set(outage_ticks)
    obs = observations.sort_values("tick").drop_duplicates("tick").set_index("tick")
    full = pd.RangeIndex(int(obs.index.min()), int(obs.index.max()) + 1, name="tick")
    d = obs["demand_liters"].reindex(full)
    observed = d.notna().to_numpy()
    d_filled = d.ffill().bfill().to_numpy(dtype=float)

    # hour of day per tick: use the observed sim_time; interpolate missing ticks from the tick clock
    sim_time = pd.to_datetime(obs["sim_time"], utc=True).reindex(full)
    anchor_tick = int(sim_time.first_valid_index())
    anchor = sim_time[anchor_tick]
    hours = np.array(
        [
            ((anchor + pd.Timedelta(minutes=(int(t) - anchor_tick) * tick_minutes)).hour)
            for t in full
        ],
        dtype=int,
    )
    ticks = np.asarray(full, dtype=int)

    theta = 2.0 * np.pi * hours / 24.0
    profile = profile_of(station_id)
    hf_table = np.array([wc.hour_factor(profile, h) for h in range(24)])
    hf = hf_table[hours]
    mult = np.array([spike_multiplier(station_id, int(t), events) for t in ticks])
    active = np.array([1.0 if spike_active(station_id, int(t), events) else 0.0 for t in ticks])

    # lags on multiplier-normalised demand (D18)
    d_norm = d_filled / mult
    ser = pd.Series(d_norm)
    e4 = ser.ewm(alpha=ALPHA_4, adjust=False).mean().shift(1).to_numpy()
    e16 = ser.ewm(alpha=ALPHA_16, adjust=False).mean().shift(1).to_numpy()
    prev1 = ser.shift(1).to_numpy()
    prev5 = ser.shift(5).to_numpy()
    prev1_raw = pd.Series(d_filled).shift(1).to_numpy()

    daily = wc.DEMAND_PROFILES[profile]["daily"][fuel_type]
    baseline_base = daily * (tick_minutes / 1440.0) * hf * region_factor(station_id)
    is_outage = np.array([(int(t) in outage) or station_outage(station_id, int(t), events) for t in ticks])

    frame = pd.DataFrame(
        {
            "tick": ticks,
            "sin_hour": np.sin(theta),
            "cos_hour": np.cos(theta),
            "ewma_4": e4,
            "ewma_16": e16,
            "momentum_4": prev1 - prev5,
            "demand_multiplier": mult,
            "event_active": active,
            "baseline": baseline_base,
            "hour_factor": hf,
            "y": d.to_numpy(dtype=float),
            "y_norm": d.to_numpy(dtype=float) / mult,
            "naive_last": prev1_raw,
            "naive_formula": baseline_base * mult,
            "outage": is_outage,
            "observed": observed,
        }
    )
    frame = frame.iloc[WARMUP_ROWS:]  # drops the EWMA warm-up and every row lacking a d(t-5)
    frame = frame[frame["observed"] & frame[FEATURES].notna().all(axis=1)]
    return frame.drop(columns=["observed"]).reset_index(drop=True)
