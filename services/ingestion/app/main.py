"""ingestion-svc FastAPI app: starts the workers in the lifespan and exposes /health, /metrics, /status."""

from __future__ import annotations

import asyncio
import contextlib
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from fsp_shared import db, migrate
from fsp_shared.bus import Bus
from fsp_shared.config import Settings, get_settings
from fsp_shared.logging import configure_logging, get_logger
from fsp_shared.metrics import install_metrics
from fsp_shared.schemas import SSEEvent
from fsp_shared.sim_client import SimClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .state import IngestionState, SafeBus
from .stream_worker import QUEUE_SIZE, EventProcessor, StreamWorker
from .sync import Syncer
from .workers import SyncCoordinator, health_probe, poll_fallback

SERVICE = "ingestion-svc"
log = get_logger("ingestion.main")


@dataclass
class Runtime:
    """Everything the running service owns (also handy for tests that drive the pieces directly)."""

    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    client: SimClient
    bus: SafeBus
    state: IngestionState
    syncer: Syncer
    coordinator: SyncCoordinator
    queue: asyncio.Queue[SSEEvent]
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    migrated: bool = False

    async def start(self) -> None:
        await self._migrate_with_retry()
        self.tasks = [
            asyncio.create_task(self.coordinator.run(), name="sync-coordinator"),
            asyncio.create_task(
                StreamWorker(self.client, self.queue, self.state, self.coordinator, self.bus).run(),
                name="sse-stream",
            ),
            asyncio.create_task(
                EventProcessor(
                    self.queue, self.syncer, self.coordinator, self.session_factory, self.bus, self.state
                ).run(),
                name="sse-processor",
            ),
            asyncio.create_task(
                poll_fallback(self.client, self.state, self.coordinator), name="poll-fallback"
            ),
            asyncio.create_task(
                health_probe(self.client, self.state, self.syncer, self.bus), name="health-probe"
            ),
        ]

    async def _migrate_with_retry(self, attempts: int = 30) -> None:
        """ingestion-svc owns migrations. Postgres may still be starting, so retry with jitter."""
        for attempt in range(1, attempts + 1):
            try:
                await migrate.upgrade_head(self.settings.DATABASE_URL)
                self.migrated = True
                log.info("migrations_applied", revision=db.HEAD_REVISION)
                return
            except Exception as exc:
                log.warning(
                    "migration_retry", attempt=attempt, error=f"{type(exc).__name__}: {str(exc)[:200]}"
                )
                if attempt == attempts:
                    raise
                await asyncio.sleep(random.uniform(0.5, 2.0))

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.client.aclose()
        if self.bus.bus is not None:
            with contextlib.suppress(Exception):
                await self.bus.bus.close()
        await self.engine.dispose()


def build_runtime(settings: Settings, *, bus: Bus | None = None, client: SimClient | None = None) -> Runtime:
    engine = db.make_engine(settings.DATABASE_URL)
    factory = db.make_session_factory(engine)
    state = IngestionState()
    safe_bus = SafeBus(bus if bus is not None else Bus.from_url(settings.REDIS_URL))
    client = client or SimClient.from_settings(settings)
    syncer = Syncer(client, factory, safe_bus, state, settings)
    coordinator = SyncCoordinator(syncer, state, settings.SYNC_MAX_HZ)
    queue: asyncio.Queue[SSEEvent] = asyncio.Queue(QUEUE_SIZE)
    return Runtime(settings, engine, factory, client, safe_bus, state, syncer, coordinator, queue)


def create_app(runtime_factory=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        configure_logging(SERVICE, settings.LOG_LEVEL)
        runtime = runtime_factory(settings) if runtime_factory else build_runtime(settings)
        app.state.runtime = runtime
        await runtime.start()
        log.info("ingestion_started", simulator=settings.SIMULATOR_URL)
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="ingestion-svc", version="0.1.0", lifespan=lifespan)
    install_metrics(app, SERVICE)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        rt: Runtime | None = getattr(app.state, "runtime", None)
        if rt is None:
            return {"status": "starting"}
        db_ok = await db.ping(rt.engine)
        return {
            "status": "ok" if db_ok else "degraded",
            "database": "ok" if db_ok else "down",
            "sse_connected": rt.state.sse_connected,
            "simulator": rt.state.sim_state.value,
        }

    @app.get("/status")
    async def status() -> dict[str, Any]:
        rt: Runtime | None = getattr(app.state, "runtime", None)
        if rt is None:
            raise HTTPException(503, "starting")
        return rt.state.to_status()

    @app.post("/sync/full", include_in_schema=False)
    async def force_full_sync() -> dict[str, Any]:
        """Ops/test helper: run a full REST sync immediately and return the tick."""
        rt: Runtime = app.state.runtime
        try:
            tick = await rt.syncer.full_sync()
        except Exception as exc:
            raise HTTPException(503, f"sync failed: {type(exc).__name__}: {exc}") from exc
        return {"tick": tick}

    return app


app = create_app()
