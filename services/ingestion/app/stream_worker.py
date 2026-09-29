"""SSE stream worker and event processor (spec §9.1.1-2).

The read loop does **no work**: it only pushes events into a bounded queue and returns to reading, because the
simulator silently drops subscribers that fall more than 200 events behind. A separate processor task drains
the queue and coalesces bursts into debounced syncs.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from fsp_shared.db import session_scope
from fsp_shared.exceptions import SimClientError, StreamFaultError
from fsp_shared.logging import get_logger
from fsp_shared.schemas import Allocation, BusEventType, SSEEvent
from fsp_shared.sim_client import SimClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import metrics as m
from . import store
from .state import IngestionState, SafeBus
from .sync import Syncer
from .workers import SyncCoordinator

log = get_logger("ingestion.stream")

QUEUE_SIZE = 1000
BACKOFF_MIN_S = 0.5
BACKOFF_MAX_S = 30.0
HEALTHY_CONNECTION_S = 5.0  # a connection that lived this long resets the backoff


def next_backoff(current: float) -> float:
    """Exponential backoff with jitter: 0.5 s -> 30 s."""
    return min(BACKOFF_MAX_S, max(BACKOFF_MIN_S, current * 2))


class StreamWorker:
    """Owns the ``/v1/stream`` connection. Reconnects forever; every (re)connect requests a full REST sync."""

    def __init__(
        self,
        client: SimClient,
        queue: asyncio.Queue[SSEEvent],
        state: IngestionState,
        coordinator: SyncCoordinator,
        bus: SafeBus,
        *,
        read_timeout: float = 45.0,
        sleep=asyncio.sleep,
    ) -> None:
        self.client = client
        self.queue = queue
        self.state = state
        self.coordinator = coordinator
        self.bus = bus
        self.read_timeout = read_timeout
        self._sleep = sleep
        self._connected_at: float | None = None
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        backoff = BACKOFF_MIN_S / 2
        while True:
            self._connected_at = None
            try:
                async for event in self.client.stream(self.read_timeout, on_connect=self._on_connect):
                    self._enqueue(event)
                # stream ended cleanly (server closed): fall through to reconnect
                log.warning("sse_stream_closed")
            except asyncio.CancelledError:
                self.state.set_sse(False)
                raise
            except StreamFaultError as exc:
                if not self.state.stream_fault:
                    self.state.stream_fault = True
                    await self.bus.publish(
                        BusEventType.SIM_FAULT, {"type": "stream_disconnect", "active": True}
                    )
                log.warning("sse_stream_fault", error=str(exc))
            except (SimClientError, OSError, ValueError) as exc:
                log.warning("sse_stream_error", error=f"{type(exc).__name__}: {exc}")
            except Exception as exc:  # noqa: BLE001 - the stream worker must never die; httpx errors land here
                log.warning("sse_stream_error", error=f"{type(exc).__name__}: {exc}")

            self.state.set_sse(False)
            self.state.sse_reconnects += 1
            m.SSE_RECONNECTS.inc()
            if (
                self._connected_at is not None
                and time.monotonic() - self._connected_at >= HEALTHY_CONNECTION_S
            ):
                backoff = BACKOFF_MIN_S / 2
            backoff = next_backoff(backoff)
            await self._sleep(random.uniform(backoff / 2, backoff))

    def _on_connect(self) -> None:
        """Called by the SSE reader as soon as the server answers 200 (even if only comments follow)."""
        self._connected_at = time.monotonic()
        self.state.set_sse(True)
        log.info("sse_connected")
        # No Last-Event-ID replay: anything missed while disconnected must come from REST.
        self.coordinator.request_full()
        if self.state.stream_fault:
            self.state.stream_fault = False
            task = asyncio.get_running_loop().create_task(
                self.bus.publish(BusEventType.SIM_FAULT, {"type": "stream_disconnect", "active": False})
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _enqueue(self, event: SSEEvent) -> None:
        self.state.last_sse_event_at = time.time()
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            m.QUEUE_DROPPED.inc()
            # We lost events; REST is the source of truth, so ask for a full resync instead of blocking the reader.
            self.coordinator.request_full()
            log.warning("sse_queue_full_dropped_event", name=event.name)


class EventProcessor:
    """Drains the SSE queue. Never blocks the reader; coalesces ticks into debounced syncs."""

    def __init__(
        self,
        queue: asyncio.Queue[SSEEvent],
        syncer: Syncer,
        coordinator: SyncCoordinator,
        session_factory: async_sessionmaker[AsyncSession],
        bus: SafeBus,
        state: IngestionState,
    ) -> None:
        self.queue = queue
        self.syncer = syncer
        self.coordinator = coordinator
        self.session_factory = session_factory
        self.bus = bus
        self.state = state

    async def run(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self.handle(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad event must not stop the processor
                log.error("sse_event_handler_failed", name=event.name, error=str(exc))

    async def handle(self, event: SSEEvent) -> None:
        name, data = event.name, event.data
        if name == "simulation.tick":
            tick = data.get("tick") if isinstance(data, dict) else None
            if isinstance(tick, int):
                self.state.note_tick_seen(tick)
                await self.bus.publish(BusEventType.TICK, {"tick": tick, "sim_time": data.get("sim_time")})
            self.coordinator.request_tick()
        elif name == "allocation.status_changed":
            await self._on_allocation(data)
        elif name == "inventory.updated":
            self.coordinator.request_tick()  # depot inventory changed: re-GET on the next debounced sync
        elif name == "simulator.notice":
            message = str(data.get("message", "")) if isinstance(data, dict) else str(data)
            log.info("simulator_notice", message=message)
            if "reset" in message.lower():
                await self.syncer.handle_reset("simulator.notice")
                self.coordinator.request_full()
        else:
            log.debug("sse_event_ignored", name=name)

    async def _on_allocation(self, data: Any) -> None:
        try:
            alloc = Allocation.model_validate(data)
        except ValidationError as exc:
            m.PAYLOAD_INVALID.labels("sse:allocation.status_changed").inc()
            log.error(
                "sse_allocation_invalid", errors=str(exc.errors(include_url=False, include_input=False))[:300]
            )
            self.coordinator.request_tick()  # fall back to REST
            return
        async with session_scope(self.session_factory) as session:
            await store.upsert_allocations(session, [alloc])
        if self.syncer.note_allocation(alloc):
            await self.syncer.publish_allocation_changed(alloc)
        self.coordinator.request_tick()  # also re-GET, as the guide requires
