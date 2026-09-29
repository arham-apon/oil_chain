"""Sync coordinator (debounced), poll fallback, simulator health probe and the retention-friendly heartbeat."""

from __future__ import annotations

import asyncio
import time

from fsp_shared.exceptions import SimClientError
from fsp_shared.logging import get_logger
from fsp_shared.schemas import BusEventType, SimulatorState
from fsp_shared.sim_client import SimClient

from . import metrics as m
from .state import IngestionState, SafeBus
from .sync import Syncer

log = get_logger("ingestion.workers")

HEARTBEAT_S = 10.0  # safety-net sync when nothing else triggers one (e.g. paused sim with events injected)
HEARTBEAT_STALE_S = 1.0  # while stale we keep re-fetching until the first clean response
POLL_INTERVAL_S = 0.5
POLL_AFTER_SSE_DOWN_S = 5.0
HEALTH_INTERVAL_S = 2.0
RETRY_DELAY_S = 0.5

_SIM_STATE_GAUGE = {SimulatorState.UP: 2, SimulatorState.DEGRADED: 1, SimulatorState.DOWN: 0}


class SyncCoordinator:
    """Debounces sync requests to at most ``SYNC_MAX_HZ``; always syncs the latest tick (each sync re-reads)."""

    def __init__(self, syncer: Syncer, state: IngestionState, max_hz: float = 4.0) -> None:
        self.syncer = syncer
        self.state = state
        self.min_interval = 1.0 / max_hz if max_hz > 0 else 0.0
        self._wake = asyncio.Event()
        self._full_requested = True  # first run is always a full sync + backfill
        self._wake.set()  # ...and it must start immediately, not after the first heartbeat
        self._last_start = 0.0
        self._done = asyncio.Event()

    def request_tick(self) -> None:
        self._wake.set()

    def request_full(self) -> None:
        self._full_requested = True
        self._wake.set()

    async def wait_for_sync(self, timeout: float = 30.0) -> None:
        """Test/ops helper: block until the next sync attempt finishes."""
        self._done.clear()
        await asyncio.wait_for(self._done.wait(), timeout)

    async def run(self) -> None:
        while True:
            timeout = HEARTBEAT_STALE_S if self.state.stale else HEARTBEAT_S
            heartbeat = False
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                heartbeat = True  # nothing happened for a while: sync anyway
            wait = self.min_interval - (time.monotonic() - self._last_start)
            if wait > 0:
                await asyncio.sleep(wait)  # debounce; requests arriving meanwhile coalesce into this sync
            self._wake.clear()
            full, self._full_requested = self._full_requested, False
            self._last_start = time.monotonic()
            try:
                if full:
                    await self.syncer.full_sync()
                else:
                    await self.syncer.tick_sync()
                    # slow sync is due every 8 ticks; on a heartbeat also catch demand history up so a paused (or
                    # slowed-down) simulator never leaves the newest ticks missing
                    if self.syncer.slow_sync_due() or (heartbeat and self.syncer.demand_behind()):
                        await self.syncer.slow_sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive, retry shortly
                log.warning("sync_failed", error=f"{type(exc).__name__}: {exc}", full=full)
                if full:
                    self._full_requested = True
                await asyncio.sleep(RETRY_DELAY_S)
                self._wake.set()
            finally:
                self._done.set()


async def poll_fallback(client: SimClient, state: IngestionState, coordinator: SyncCoordinator) -> None:
    """If SSE has been down > 5 s, poll ``/v1/instance`` every 500 ms so the platform keeps working without SSE."""
    while True:
        await asyncio.sleep(POLL_INTERVAL_S)
        if state.sse_down_for() <= POLL_AFTER_SSE_DOWN_S:
            continue
        try:
            inst = (await client.instance()).data
        except SimClientError:
            continue  # sync/health will surface the outage; keep polling
        changed = inst.tick != state.last_synced_tick or inst.status != state.sim_status
        state.note_tick_seen(inst.tick)
        if changed:
            coordinator.request_tick()


async def health_probe(client: SimClient, state: IngestionState, syncer: Syncer, bus: SafeBus) -> None:
    """``/v1/health`` every 2 s (bypasses faults): UP / DEGRADED / DOWN."""
    while True:
        await asyncio.sleep(HEALTH_INTERVAL_S)
        previous = state.sim_state
        current = await client.probe_state()
        state.sim_state = current
        m.SIM_STATUS.set(_SIM_STATE_GAUGE[current])
        if current is not previous:
            log.warning("simulator_state_changed", previous=previous.value, current=current.value)
            await bus.publish(BusEventType.SIM_FAULT, {"type": "simulator_state", "state": current.value})
            if current is not SimulatorState.UP:
                await syncer.raise_alert(
                    f"SIMULATOR_{current.value}",
                    "HIGH" if current is SimulatorState.DOWN else "MEDIUM",
                    "Simulator unreachable"
                    if current is SimulatorState.DOWN
                    else "Simulator /v1/* calls are failing while /v1/health answers (fault active)",
                )
