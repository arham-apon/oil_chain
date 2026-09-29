"""Demand-generator knowledge shared by forecast-svc and decision-svc (local fallback).

Empirically verified against the simulator image (docs/MODEL_REPORT.md, spec §10.1):

* ``demand(s,f,t) = daily[profile][f] * tick_minutes/1440 * hour_factor(profile, hour(sim_time_t)) * region_factor
  * demand_multiplier(s,t) * jitter`` with mean-1 multiplicative jitter of std ~ noise/sqrt(3).
* The hour comes from the ``sim_time`` of the row's own tick.
* A ``demand_spike`` (and any event) covers ticks ``start_tick <= t <= end_tick`` — the end tick is INCLUSIVE.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from . import world_constants as wc
from .schemas import HorizonPoint
from .timeutil import project_sim_time

DEFAULT_SPIKE_MULTIPLIER = 1.5  # simulator default when the event carries no ``multiplier``


class EventLike(Protocol):
    type: str
    start_tick: int
    end_tick: int
    parameters: Mapping[str, Any] | None


@dataclass(frozen=True)
class EventSpec:
    """Minimal event record (usable for ORM rows, API models and tests alike)."""

    type: str
    start_tick: int
    end_tick: int
    parameters: Mapping[str, Any] = field(default_factory=dict)
    status: str = "ACTIVE"


def covers_tick(event: EventLike, tick: int) -> bool:
    return event.start_tick <= tick <= event.end_tick


def covers_entity(event: EventLike, entity_id: str, region_id: str | None, *, id_key: str) -> bool:
    """Empty ``<x>_ids`` and ``region_ids`` filters mean "all"; otherwise the entity or its region must match."""
    params = event.parameters or {}
    ids = params.get(id_key) or []
    regions = params.get("region_ids") or []
    if not ids and not regions:
        return True
    return entity_id in ids or (region_id is not None and region_id in regions)


def region_of(station_id: str) -> str | None:
    st = wc.STATIONS.get(station_id)
    return st["region_id"] if st else None


def spike_multiplier(station_id: str, tick: int, events: Iterable[EventLike]) -> float:
    """Product of the multipliers of every demand_spike covering (station, tick)."""
    region = region_of(station_id)
    mult = 1.0
    for e in events:
        if (
            e.type == "demand_spike"
            and covers_tick(e, tick)
            and covers_entity(e, station_id, region, id_key="station_ids")
        ):
            mult *= float((e.parameters or {}).get("multiplier", DEFAULT_SPIKE_MULTIPLIER))
    return mult


def spike_active(station_id: str, tick: int, events: Iterable[EventLike]) -> bool:
    region = region_of(station_id)
    return any(
        e.type == "demand_spike"
        and covers_tick(e, tick)
        and covers_entity(e, station_id, region, id_key="station_ids")
        for e in events
    )


def station_outage(station_id: str, tick: int, events: Iterable[EventLike]) -> bool:
    region = region_of(station_id)
    return any(
        e.type == "station_outage"
        and covers_tick(e, tick)
        and covers_entity(e, station_id, region, id_key="station_ids")
        for e in events
    )


def region_factor(station_id: str) -> float:
    return float(wc.REGIONS[region_of(station_id) or "region-dhaka"]["demand_factor"])


def profile_of(station_id: str) -> str:
    return str(wc.STATIONS[station_id]["demand_profile"])


def noise_of(station_id: str) -> float:
    return float(wc.DEMAND_PROFILES[profile_of(station_id)]["noise"])


def baseline_demand_at(
    station_id: str,
    fuel_type: str,
    hour: int,
    tick_minutes: int,
    multiplier: float = 1.0,
) -> float:
    """Expected demand for one tick (spec §10.1 formula)."""
    return wc.baseline_demand(
        profile_of(station_id), fuel_type, hour, tick_minutes, region_factor(station_id), multiplier
    )


def baseline_horizon(
    station_id: str,
    fuel_type: str,
    current_tick: int,
    current_sim_time: str | datetime,
    tick_minutes: int,
    horizon: int,
    events: Iterable[EventLike] = (),
) -> list[HorizonPoint]:
    """Deterministic fallback forecast for ticks ``current_tick+1 .. current_tick+horizon`` with
    ``sigma = noise x baseline`` (spec §10.6). Scheduled/active spikes multiply the baseline."""
    events = list(events)
    points = []
    for k in range(1, horizon + 1):
        t = current_tick + k
        hour = project_sim_time(current_sim_time, current_tick, t, tick_minutes).hour
        mean = baseline_demand_at(
            station_id, fuel_type, hour, tick_minutes, spike_multiplier(station_id, t, events)
        )
        points.append(HorizonPoint(tick=t, mean=mean, sigma=noise_of(station_id) * mean))
    return points
