"""forecast-svc FastAPI app (spec §10.8): /forecast, /train, /models, /health, /metrics."""

from __future__ import annotations

import asyncio
import contextlib
import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field

from fsp_shared import db, world_constants as wc
from fsp_shared.bus import Bus
from fsp_shared.config import Settings, get_settings
from fsp_shared.logging import configure_logging, get_logger
from fsp_shared.metrics import install_metrics
from fsp_shared.schemas import BusEventType

from . import metrics as m
from .forecaster import Forecaster, NoStateError
from .state import StateCache
from .trainer import ModelHolder, Trainer, TrainingError

SERVICE = "forecast-svc"
log = get_logger("forecast.main")
REFRESH_INTERVAL_S = 0.5  # polling fallback (used when Redis is unavailable)
REFRESH_MIN_GAP_S = 0.05
WAKE_EVENTS = {
    BusEventType.STATE_UPDATED,
    BusEventType.EVENT_CHANGED,
    BusEventType.ALLOCATION_CHANGED,
    BusEventType.SIM_RESET,
}
TRAIN_CHECK_INTERVAL_S = 2.0


class ForecastRequest(BaseModel):
    station_ids: list[str] | None = None
    fuel_types: list[str] | None = None
    horizon: int | None = Field(default=None, ge=1, le=96)


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = db.make_engine(settings.DATABASE_URL, pool_size=5)
        self.cache = StateCache(self.engine)
        self.holder = ModelHolder()
        self.trainer = Trainer(self.engine, Path(settings.MODEL_DIR), self.holder)
        self.forecaster = Forecaster(self.cache, self.holder, settings.HORIZON_TICKS)
        self.tasks: list[asyncio.Task[Any]] = []
        self._wake = asyncio.Event()
        self.bus: Bus | None = None

    async def start(self) -> None:
        await db.wait_for_schema(self.engine, timeout_s=180)
        await self.trainer.load_active()
        with contextlib.suppress(Exception):
            await self.cache.refresh()
        self.tasks = [
            asyncio.create_task(self._refresh_loop(), name="state-refresh"),
            asyncio.create_task(self._train_loop(), name="train-scheduler"),
            asyncio.create_task(self._bus_listener(), name="bus-listener"),
        ]

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self.bus is not None:
            with contextlib.suppress(Exception):
                await self.bus.close()
        await self.engine.dispose()

    async def _bus_listener(self) -> None:
        """Wake the refresher as soon as ingestion publishes a change (Redis). Failure only costs latency:
        the refresher keeps polling every REFRESH_INTERVAL_S."""
        backoff = 0.5
        while True:
            try:
                self.bus = self.bus or Bus.from_url(self.settings.REDIS_URL)
                async for msg in self.bus.listen("forecast-svc", "forecast-1"):
                    backoff = 0.5
                    if msg.type in WAKE_EVENTS:
                        self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Redis down/restarting: retry with backoff
                log.warning("bus_listener_error", error=f"{type(exc).__name__}: {str(exc)[:150]}")
                self.bus = None
                await asyncio.sleep(random.uniform(backoff / 2, backoff))
                backoff = min(30.0, backoff * 2)

    async def _refresh_loop(self) -> None:
        while True:
            try:
                world = await self.cache.refresh()
                if world is not None:
                    m.STATE_AGE.set(world.age_s())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep serving the last snapshot while Postgres recovers
                log.warning("state_refresh_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
                if self.cache.world is not None:
                    m.STATE_AGE.set(self.cache.world.age_s())
            try:
                await asyncio.wait_for(self._wake.wait(), REFRESH_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            await asyncio.sleep(REFRESH_MIN_GAP_S)  # coalesce bursts of events into one refresh

    async def _train_loop(self) -> None:
        while True:
            await asyncio.sleep(TRAIN_CHECK_INTERVAL_S)
            world = self.cache.world
            if world is None:
                continue
            min_rows = min((p.n_rows for p in world.pairs.values()), default=0)
            if not self.trainer.should_retrain(world.tick, min_rows):
                continue
            try:
                await self.trainer.train()
            except asyncio.CancelledError:
                raise
            except TrainingError as exc:
                log.info("training_skipped", reason=str(exc)[:200])
            except Exception as exc:  # noqa: BLE001
                log.error("training_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
                await asyncio.sleep(30)


def create_app(runtime_factory=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        configure_logging(SERVICE, settings.LOG_LEVEL)
        rt = runtime_factory(settings) if runtime_factory else Runtime(settings)
        app.state.runtime = rt
        await rt.start()
        log.info("forecast_started", model=rt.holder.bundle.version if rt.holder.bundle else None)
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title="forecast-svc", version="0.1.0", lifespan=lifespan)
    install_metrics(app, SERVICE)

    def rt() -> Runtime:
        return app.state.runtime

    @app.get("/health")
    async def health() -> dict[str, Any]:
        r: Runtime | None = getattr(app.state, "runtime", None)
        if r is None:
            return {"status": "starting"}
        db_ok = await db.ping(r.engine)
        bundle = r.holder.bundle
        return {
            "status": "ok" if db_ok else "degraded",
            "database": "ok" if db_ok else "down",
            "state": r.cache.info(),
            "model": None if bundle is None else {"version": bundle.version, "trained_at_tick": bundle.trained_at_tick},
            "mode": "MODEL" if bundle else "BASELINE_FALLBACK",
        }

    def _serve(station_ids: list[str] | None, fuel_types: list[str] | None, horizon: int | None) -> Response:
        for s in station_ids or []:
            if s not in wc.STATIONS:
                raise HTTPException(422, f"unknown station_id {s!r}")
        for f in fuel_types or []:
            if f not in wc.FUEL_TYPES:
                raise HTTPException(422, f"unknown fuel_type {f!r}")
        try:
            body, world = rt().forecaster.forecast_json(station_ids, fuel_types, horizon)
        except NoStateError as exc:
            raise HTTPException(503, str(exc)) from exc
        return Response(body, media_type="application/json", headers={"X-State-Age": f"{world.age_s():.2f}"})

    @app.post("/forecast")
    async def forecast(req: ForecastRequest | None = None) -> Response:
        req = req or ForecastRequest()
        return _serve(req.station_ids, req.fuel_types, req.horizon)

    @app.get("/forecast")
    async def forecast_get(
        station_id: list[str] | None = Query(default=None),
        fuel_type: list[str] | None = Query(default=None),
        horizon: int | None = Query(default=None, ge=1, le=96),
    ) -> Response:
        """Same as POST /forecast, for curl/browsers/clients that dislike request bodies."""
        return _serve(station_id, fuel_type, horizon)

    @app.post("/train")
    async def train() -> dict[str, Any]:
        try:
            report = await rt().trainer.train()
        except TrainingError as exc:
            raise HTTPException(409, str(exc)) from exc
        return report.to_dict()

    @app.get("/models")
    async def models() -> dict[str, Any]:
        r = rt()
        bundle = r.holder.bundle
        active = None
        if bundle is not None:
            active = {
                "version": bundle.version,
                "trained_at_tick": bundle.trained_at_tick,
                "mean_mae": bundle.mean_mae,
                "mean_baseline_mae": bundle.mean_baseline_mae,
                "pairs": {
                    f"{s}/{f}": {k: v for k, v in pm.metrics.items() if k != "alpha_grid_mae"}
                    for (s, f), pm in bundle.pairs.items()
                },
            }
        return {"active": active, "registry": await r.trainer.list_models()}

    return app


app = create_app()
