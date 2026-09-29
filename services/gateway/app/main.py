"""gateway-svc FastAPI app (spec §13.1): REST + WebSocket backend-for-frontend for the operator UI."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fsp_shared import db
from fsp_shared.config import get_settings
from fsp_shared.logging import configure_logging, get_logger
from fsp_shared.metrics import install_metrics

from .routers import audit, copilot, decisions, demo, health, state
from .runtime import Runtime

SERVICE = "gateway-svc"
log = get_logger("gateway.main")


def create_app(runtime_factory=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        configure_logging(SERVICE, settings.LOG_LEVEL)
        rt = runtime_factory(settings) if runtime_factory else Runtime(settings)
        app.state.runtime = rt
        await rt.start()
        log.info("gateway_started", demo_controls=settings.DEMO_CONTROLS)
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title="gateway-svc", version="0.1.0", lifespan=lifespan,
                  description="Backend-for-frontend of the Fuel Supply Operations Platform. All data is simulated.")
    install_metrics(app, SERVICE)
    # the UI is served same-origin (nginx / Vite proxy); CORS only matters for the Vite dev server on another port
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://localhost:3000"],
                       allow_methods=["*"], allow_headers=["*"])
    for r in (state.router, decisions.router, copilot.router, health.router, audit.router, demo.router):
        app.include_router(r)

    @app.get("/health")
    async def health_ep() -> dict[str, Any]:
        rt: Runtime | None = getattr(app.state, "runtime", None)
        if rt is None:
            return {"status": "starting"}
        ok = await db.ping(rt.engine)
        return {"status": "ok" if ok else "degraded", "database": "ok" if ok else "down",
                "ws_clients": len(rt.hub.clients), "bus": rt.hub.bus_connected}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        rt: Runtime = app.state.runtime
        await rt.hub.connect(websocket)
        try:
            while True:  # the client only sends pings; everything else is server -> client
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            rt.hub.disconnect(websocket)

    return app


app = create_app()
