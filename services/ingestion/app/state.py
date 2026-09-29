"""Runtime state shared by the ingestion workers, and a publish wrapper that never raises."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from fsp_shared.bus import Bus
from fsp_shared.logging import get_logger
from fsp_shared.schemas import SimulatorState

from . import metrics as m

log = get_logger("ingestion.state")


@dataclass
class IngestionState:
    started_at: float = field(default_factory=time.time)
    # --- SSE -----------------------------------------------------------------------------
    sse_connected: bool = False
    sse_down_since: float = field(default_factory=time.monotonic)
    sse_reconnects: int = 0
    last_sse_event_at: float | None = None
    # --- sync bookkeeping ---------------------------------------------------------------
    last_synced_tick: int | None = None
    latest_tick_seen: int | None = None
    tick_minutes: int = 15
    sim_status: str | None = None  # RUNNING / PAUSED, from /v1/instance
    last_sync_at: float | None = None
    last_sync_latency_ms: float | None = None
    last_sync_ok: bool | None = None
    sync_failures: int = 0
    last_slow_tick: int | None = None
    last_demand_tick: int | None = None
    needs_backfill: bool = True
    station_ids: list[str] = field(default_factory=list)
    # --- faults / health ---------------------------------------------------------------
    stale: bool = False
    stale_since_tick: int | None = None
    sim_state: SimulatorState = SimulatorState.UP
    stream_fault: bool = False
    # --- caches ------------------------------------------------------------------------
    last_good: dict[str, Any] = field(default_factory=dict)  # last NON-stale, valid payloads
    prev_events: dict[int, tuple[str, int]] | None = None  # id -> (status, end_tick)
    prev_allocations: dict[int, str] | None = None  # id -> status
    demand_gaps: dict[str, int] = field(default_factory=dict)

    def note_tick_seen(self, tick: int) -> None:
        if (
            self.latest_tick_seen is None
            or tick > self.latest_tick_seen
            or (self.last_synced_tick is not None and tick < self.last_synced_tick)
        ):
            self.latest_tick_seen = tick
        self.update_lag()

    def update_lag(self) -> None:
        if self.latest_tick_seen is not None and self.last_synced_tick is not None:
            m.SYNC_LAG_TICKS.set(max(0, self.latest_tick_seen - self.last_synced_tick))

    def set_sse(self, connected: bool) -> None:
        if connected and not self.sse_connected:
            self.sse_connected = True
        elif not connected and self.sse_connected:
            self.sse_connected = False
            self.sse_down_since = time.monotonic()
        m.SSE_CONNECTED.set(1 if self.sse_connected else 0)

    def sse_down_for(self) -> float:
        return 0.0 if self.sse_connected else time.monotonic() - self.sse_down_since

    def to_status(self) -> dict[str, Any]:
        return {
            "sse": {
                "connected": self.sse_connected,
                "reconnects": self.sse_reconnects,
                "down_for_s": round(self.sse_down_for(), 1),
                "stream_fault": self.stream_fault,
                "last_event_age_s": None
                if self.last_sse_event_at is None
                else round(time.time() - self.last_sse_event_at, 1),
            },
            "last_synced_tick": self.last_synced_tick,
            "latest_tick_seen": self.latest_tick_seen,
            "sync_lag_ticks": None
            if self.latest_tick_seen is None or self.last_synced_tick is None
            else max(0, self.latest_tick_seen - self.last_synced_tick),
            "last_sync_latency_ms": self.last_sync_latency_ms,
            "last_sync_ok": self.last_sync_ok,
            "last_sync_age_s": None
            if self.last_sync_at is None
            else round(time.time() - self.last_sync_at, 1),
            "sync_failures": self.sync_failures,
            "stale": self.stale,
            "simulator": {"state": self.sim_state.value, "status": self.sim_status},
            "demand_gaps": self.demand_gaps,
            "uptime_s": round(time.time() - self.started_at, 1),
        }


class SafeBus:
    """Publishes to Redis but never raises: a Redis outage must not stop ingestion (spec §14.1)."""

    def __init__(self, bus: Bus | None) -> None:
        self.bus = bus

    async def publish(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.publish(event_type, payload)
        except Exception as exc:  # noqa: BLE001 - any Redis/network error is non-fatal here
            m.BUS_PUBLISH_ERRORS.inc()
            log.warning("bus_publish_failed", type=event_type, error=str(exc))
