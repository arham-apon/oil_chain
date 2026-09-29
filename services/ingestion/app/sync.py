"""Tick sync and slow sync (spec §9.1.3-4), stale-data handling (§9.2) and the malformed-data guard (§9.3).

REST is the source of truth: every sync re-GETs the world and upserts it, tagged with the tick read from
``/v1/instance`` at the start of that sync.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fsp_shared.config import Settings
from fsp_shared.db import session_scope
from fsp_shared.exceptions import SimClientError, SimPayloadError
from fsp_shared.logging import bind_context, get_logger
from fsp_shared.schemas import Allocation, BusEventType, Instance, SimEvent
from fsp_shared.sim_client import SimClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import metrics as m
from . import store
from .demand_backfill import DemandSyncer
from .state import IngestionState, SafeBus

log = get_logger("ingestion.sync")

CORE = ("instance", "depots", "stations", "routes")  # a sync without these would give inconsistent snapshots
ALL_TICK_ENDPOINTS = ("instance", "depots", "stations", "routes", "events", "allocations", "metrics")
SLOW_SYNC_EVERY_TICKS = 8
RETENTION_EVERY_TICKS = 50
INVALID_ALERT_MIN_INTERVAL_S = 30.0


class SyncError(Exception):
    """A tick sync could not complete (core endpoints unavailable)."""


class Syncer:
    def __init__(
        self,
        client: SimClient,
        session_factory: async_sessionmaker[AsyncSession],
        bus: SafeBus,
        state: IngestionState,
        settings: Settings,
    ) -> None:
        self.client = client
        self.session_factory = session_factory
        self.bus = bus
        self.state = state
        self.settings = settings
        self._lock = asyncio.Lock()  # single-flight: only one sync touches the DB at a time
        self._last_invalid_alert: dict[str, float] = {}
        self.demand = DemandSyncer(client, session_factory, state, self.on_invalid_payload)

    # ------------------------------------------------------------------------------------------
    # alerts / invalid payloads
    # ------------------------------------------------------------------------------------------
    async def raise_alert(
        self,
        kind: str,
        severity: str,
        message: str,
        *,
        entity_id: str | None = None,
        data: dict[str, Any] | None = None,
        tick: int | None = None,
    ) -> None:
        tick = tick if tick is not None else self.state.last_synced_tick
        try:
            async with session_scope(self.session_factory) as session:
                alert_id = await store.insert_alert(
                    session,
                    tick=tick,
                    kind=kind,
                    severity=severity,
                    message=message,
                    entity_id=entity_id,
                    data=data,
                )
        except Exception as exc:  # noqa: BLE001 - alerting must never break ingestion
            log.error("alert_persist_failed", kind=kind, error=str(exc))
            alert_id = None
        m.ALERTS.labels(kind).inc()
        log.warning("alert_raised", kind=kind, severity=severity, message=message, entity_id=entity_id)
        await self.bus.publish(
            BusEventType.ALERT,
            {
                "id": alert_id,
                "tick": tick,
                "kind": kind,
                "severity": severity,
                "message": message,
                "entity_id": entity_id,
            },
        )

    async def on_invalid_payload(self, endpoint: str, exc: SimPayloadError) -> None:
        """Reject the payload, keep the last valid cache, alert the operator (rate-limited per endpoint)."""
        m.PAYLOAD_INVALID.labels(endpoint).inc()
        now = time.monotonic()
        last = self._last_invalid_alert.get(endpoint)
        log.error("sim_payload_invalid", endpoint=endpoint, errors=str(exc.errors)[:500])
        if last is not None and now - last < INVALID_ALERT_MIN_INTERVAL_S:
            return
        self._last_invalid_alert[endpoint] = now
        await self.raise_alert(
            "DATA_INVALID",
            "HIGH",
            f"Simulator payload from {endpoint} failed validation; using last valid data",
            entity_id=endpoint,
            data={"errors": str(exc.errors)[:1000]},
        )

    # ------------------------------------------------------------------------------------------
    # change detection (also used by the SSE event processor)
    # ------------------------------------------------------------------------------------------
    def note_allocation(self, alloc: Allocation) -> bool:
        """Record an allocation's status; True if it is new or changed (and the cache was primed)."""
        prev = self.state.prev_allocations
        if prev is None:
            self.state.prev_allocations = prev = {}
            first_population = True
        else:
            first_population = False
        changed = prev.get(alloc.id) != alloc.status
        prev[alloc.id] = alloc.status
        return changed and not first_population

    async def publish_allocation_changed(self, alloc: Allocation) -> None:
        await self.bus.publish(
            BusEventType.ALLOCATION_CHANGED,
            {
                "id": alloc.id,
                "status": alloc.status,
                "idempotency_key": alloc.idempotency_key,
                "route_id": alloc.route_id,
                "failure_reason": alloc.failure_reason,
            },
        )

    def _event_changes(self, events: list[SimEvent]) -> list[dict[str, Any]]:
        prev = self.state.prev_events
        current: dict[int, tuple[str, int]] = {e.id: (e.status, e.end_tick) for e in events}
        self.state.prev_events = current
        if prev is None:  # first population: nothing "changed"
            return []
        out = []
        for e in events:
            before = prev.get(e.id)
            if before is None:
                change = "new"
            elif before[0] != e.status:
                change = "status"
            else:
                continue
            out.append(
                {
                    "id": e.id,
                    "type": e.type,
                    "status": e.status,
                    "start_tick": e.start_tick,
                    "end_tick": e.end_tick,
                    "change": change,
                    "parameters": e.parameters,
                }
            )
        return out

    # ------------------------------------------------------------------------------------------
    # stale-data handling
    # ------------------------------------------------------------------------------------------
    async def _update_stale(self, stale: bool, tick: int) -> None:
        was = self.state.stale
        self.state.stale = stale
        m.SIM_STALE.set(1 if stale else 0)
        if stale and not was:
            self.state.stale_since_tick = tick
            await self.raise_alert(
                "STALE_DATA",
                "HIGH",
                "Simulator is serving stale data (X-Simulator-Stale: true); auto-commit suspended",
                tick=tick,
            )
            await self.bus.publish(
                BusEventType.SIM_FAULT, {"type": "stale_data", "active": True, "tick": tick}
            )
        elif was and not stale:
            self.state.stale_since_tick = None
            log.info("stale_data_cleared", tick=tick)
            await self.bus.publish(
                BusEventType.SIM_FAULT, {"type": "stale_data", "active": False, "tick": tick}
            )

    # ------------------------------------------------------------------------------------------
    # tick sync
    # ------------------------------------------------------------------------------------------
    async def tick_sync(self) -> int:
        async with self._lock:
            return await self._tick_sync_locked()

    async def _tick_sync_locked(self) -> int:
        started = time.perf_counter()
        try:
            tick = await self._do_tick_sync()
        except BaseException as exc:
            m.SYNCS.labels("tick", "false").inc()
            if not isinstance(exc, asyncio.CancelledError):
                self.state.sync_failures += 1
                self.state.last_sync_ok = False
            raise
        finally:
            m.SYNC_DURATION.labels("tick").observe(time.perf_counter() - started)
        m.SYNCS.labels("tick", "true").inc()
        self.state.last_sync_ok = True
        self.state.last_sync_at = time.time()
        self.state.last_sync_latency_ms = round((time.perf_counter() - started) * 1000, 1)
        return tick

    async def _do_tick_sync(self) -> int:
        client = self.client
        results = await asyncio.gather(
            *(getattr(client, name)() for name in ALL_TICK_ENDPOINTS), return_exceptions=True
        )
        got: dict[str, Any] = {}
        failures: dict[str, BaseException] = {}
        for name, res in zip(ALL_TICK_ENDPOINTS, results, strict=True):
            if isinstance(res, SimPayloadError):
                await self.on_invalid_payload(f"/v1/{name}", res)
                failures[name] = res
            elif isinstance(res, SimClientError):
                failures[name] = res
            elif isinstance(res, BaseException):
                raise res
            else:
                got[name] = res
        missing = [n for n in CORE if n not in got]
        if missing:
            detail = {n: str(failures[n])[:200] for n in missing}
            raise SyncError(f"core endpoints unavailable: {detail}")
        optional_failed = [n for n in failures if n not in CORE]
        if optional_failed:
            log.warning("tick_sync_partial", failed=optional_failed)

        inst: Instance = got["instance"].data
        tick = inst.tick
        bind_context(tick=tick)
        stale = any(r.stale for r in got.values())
        st = self.state
        st.tick_minutes = inst.tick_minutes
        st.sim_status = inst.status
        st.station_ids = [s.id for s in got["stations"].data]

        if st.last_synced_tick is not None and tick < st.last_synced_tick:
            log.warning("tick_regressed", previous=st.last_synced_tick, now=tick)
            await self._reset_locked("tick_regressed")

        depots, stations, routes = got["depots"].data, got["stations"].data, got["routes"].data
        events: list[SimEvent] = got["events"].data if "events" in got else []
        allocations: list[Allocation] = got["allocations"].data if "allocations" in got else []
        async with session_scope(self.session_factory) as session:
            await store.upsert_tick(session, inst, stale)
            await store.upsert_depots(session, tick, depots, stale)
            await store.upsert_stations(session, tick, stations, stale)
            await store.upsert_routes(session, tick, routes)
            if "events" in got:
                await store.upsert_events(session, tick, events)
            if "allocations" in got:
                await store.upsert_allocations(session, allocations)
            if "metrics" in got:
                await store.upsert_metrics(session, tick, got["metrics"].data)
            if tick % RETENTION_EVERY_TICKS == 0:
                await store.apply_retention(session, tick, self.settings.SNAPSHOT_RETENTION_TICKS)

        st.last_synced_tick = tick
        st.note_tick_seen(tick)
        if not stale:  # keep the last known-good payloads (spec §9.2)
            st.last_good = {name: r.data for name, r in got.items()}
            st.last_good["tick"] = tick
        m.SIM_TICK.set(tick)
        if "metrics" in got:
            mt = got["metrics"].data
            m.SIM_SERVICE_LEVEL.set(mt.service_level)
            m.SIM_UNMET_LITERS.set(mt.unmet_demand_liters)
            m.SIM_ALLOCATION_FAILURES.set(mt.allocation_failures)

        await self._update_stale(stale, tick)
        await self.bus.publish(BusEventType.STATE_UPDATED, {"tick": tick, "stale": stale})
        for change in self._event_changes(events):
            await self.bus.publish(BusEventType.EVENT_CHANGED, change)
        if "allocations" in got:
            for alloc in allocations:
                if self.note_allocation(alloc):
                    await self.publish_allocation_changed(alloc)
            if st.prev_allocations is None:  # first sync saw no allocations: cache is primed (empty)
                st.prev_allocations = {}
        return tick

    # ------------------------------------------------------------------------------------------
    # slow sync (supply arrivals + demand history)
    # ------------------------------------------------------------------------------------------
    async def slow_sync(self, *, backfill: bool | None = None) -> bool:
        async with self._lock:
            return await self._slow_sync_locked(backfill)

    async def _slow_sync_locked(self, backfill: bool | None) -> bool:
        started = time.perf_counter()
        st = self.state
        tick = st.last_synced_tick if st.last_synced_tick is not None else 0
        do_backfill = st.needs_backfill if backfill is None else backfill
        ok = True
        try:
            try:
                supply = await self.client.supply_arrivals()
                if not supply.stale:
                    async with session_scope(self.session_factory) as session:
                        await store.upsert_supply(session, supply.data)
            except SimPayloadError as exc:
                ok = False
                await self.on_invalid_payload("/v1/supply-arrivals", exc)
            except SimClientError as exc:
                ok = False
                log.warning("supply_sync_failed", error=str(exc))
            ok = await self.demand.sync(tick, backfill=do_backfill) and ok
            if ok:
                st.last_slow_tick = tick
                if do_backfill:
                    st.needs_backfill = False
        finally:
            m.SYNC_DURATION.labels("slow").observe(time.perf_counter() - started)
            m.SYNCS.labels("slow", "true" if ok else "false").inc()
        return ok

    def slow_sync_due(self) -> bool:
        st = self.state
        if st.last_slow_tick is None or st.last_synced_tick is None:
            return True
        return st.last_synced_tick - st.last_slow_tick >= SLOW_SYNC_EVERY_TICKS

    def demand_behind(self) -> bool:
        """True when newer ticks were synced than demand history covers (slow sync only runs every 8 ticks)."""
        st = self.state
        return (
            st.last_demand_tick is not None
            and st.last_synced_tick is not None
            and st.last_synced_tick > st.last_demand_tick
        )

    async def full_sync(self) -> int:
        """Everything: tick sync + supply arrivals + demand history (backfill when flagged)."""
        tick = await self.tick_sync()
        await self.slow_sync()
        return tick

    # ------------------------------------------------------------------------------------------
    # simulator reset
    # ------------------------------------------------------------------------------------------
    async def _reset_locked(self, reason: str) -> None:
        log.warning("simulator_reset_detected", reason=reason)
        async with session_scope(self.session_factory) as session:
            await store.wipe_mirror_tables(session)
        st = self.state
        st.last_synced_tick = None
        st.latest_tick_seen = None
        st.last_slow_tick = None
        st.last_demand_tick = None
        st.needs_backfill = True
        st.prev_events = None
        st.prev_allocations = None
        st.last_good = {}
        st.demand_gaps = {}
        await self.bus.publish(BusEventType.SIM_RESET, {"reason": reason})

    async def handle_reset(self, reason: str = "simulator.notice") -> None:
        async with self._lock:
            await self._reset_locked(reason)
