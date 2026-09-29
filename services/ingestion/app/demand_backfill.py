"""Demand-history ingestion: startup backfill (limit 2000 per station) and incremental slow-sync (spec §9.1.4)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fsp_shared.db import session_scope
from fsp_shared.exceptions import SimClientError, SimPayloadError
from fsp_shared.logging import get_logger
from fsp_shared.schemas import DemandRow
from fsp_shared.sim_client import SimClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import store
from .state import IngestionState

log = get_logger("ingestion.demand")

MAX_LIMIT = 2000
FUELS_PER_STATION = 3
OnInvalid = Callable[[str, SimPayloadError], Awaitable[None]]


def incremental_limit(ticks_since_last: int) -> int:
    """``min(2000, 3 x ticks_since_last + 30)`` — 3 fuels per station per tick, plus a safety margin."""
    return min(MAX_LIMIT, FUELS_PER_STATION * max(0, ticks_since_last) + 30)


class DemandSyncer:
    def __init__(
        self,
        client: SimClient,
        session_factory: async_sessionmaker[AsyncSession],
        state: IngestionState,
        on_invalid: OnInvalid,
    ) -> None:
        self.client = client
        self.session_factory = session_factory
        self.state = state
        self.on_invalid = on_invalid

    async def sync(self, current_tick: int, *, backfill: bool = False) -> bool:
        """Pull demand history for every known station. Returns True when every station succeeded."""
        stations = self.state.station_ids
        if not stations:
            return True
        if backfill:
            limit = MAX_LIMIT
        else:
            since = current_tick - (
                self.state.last_demand_tick if self.state.last_demand_tick is not None else 0
            )
            limit = incremental_limit(since)
            if FUELS_PER_STATION * since + 30 > MAX_LIMIT:
                log.warning("demand_history_gap_risk", ticks_since_last=since, limit=limit)

        results = await asyncio.gather(
            *(self.client.demand_history(s, limit) for s in stations), return_exceptions=True
        )
        ok = True
        rows: list[DemandRow] = []
        for station, res in zip(stations, results, strict=True):
            if isinstance(res, SimPayloadError):
                ok = False
                await self.on_invalid(f"/v1/demand-history[{station}]", res)
            elif isinstance(res, SimClientError):
                ok = False
                log.warning("demand_history_failed", station=station, error=str(res))
            elif isinstance(res, BaseException):
                raise res
            else:
                if res.stale:
                    continue  # never persist history that came from a stale response
                rows.extend(res.data)
        if rows:
            async with session_scope(self.session_factory) as session:
                top = await store.insert_demand(session, rows)
                self.state.demand_gaps = await store.demand_gaps(session)
            self.state.last_demand_tick = max(self.state.last_demand_tick or -1, top)
            log.info(
                "demand_synced",
                rows=len(rows),
                backfill=backfill,
                limit=limit,
                last_demand_tick=self.state.last_demand_tick,
                gaps=self.state.demand_gaps or None,
            )
        return ok
