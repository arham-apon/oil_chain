"""Tick <-> wall-clock helpers. Always derive the hour of day from ``sim_time`` (never tick math)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def parse_sim_time(value: str | datetime) -> datetime:
    """Parse a simulator ``sim_time``. The live image emits naive ISO strings; treat them as UTC."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def hour_of_day(sim_time: str | datetime) -> int:
    return parse_sim_time(sim_time).hour


def ticks_per_hour(tick_minutes: float) -> float:
    """C19: burn-rate conversions must use the live ``tick_minutes`` (not a hardcoded 4)."""
    if tick_minutes <= 0:
        raise ValueError("tick_minutes must be positive")
    return 60.0 / tick_minutes


def project_sim_time(
    current_sim_time: str | datetime, current_tick: int, target_tick: int, tick_minutes: int
) -> datetime:
    """Sim time at ``target_tick`` extrapolated from a known (tick, sim_time) anchor."""
    return parse_sim_time(current_sim_time) + timedelta(minutes=(target_tick - current_tick) * tick_minutes)
