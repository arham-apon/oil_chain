"""cognitive-svc FastAPI app (spec §12.5): /explain, /incident, /copilot/chat, /health, /metrics."""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fsp_shared import db
from fsp_shared.breaker import CircuitBreaker
from fsp_shared.config import Settings, get_settings
from fsp_shared.logging import configure_logging, get_logger
from fsp_shared.metrics import install_metrics
from pydantic import BaseModel
from sqlalchemy import text

from .copilot import Copilot
from .explain import explain
from .incident import build_brief
from .llm import GeminiLLM
from .tools import ToolBox

SERVICE = "cognitive-svc"
log = get_logger("cognitive.main")


class ExplainBody(BaseModel):
    decision_id: str


class IncidentBody(BaseModel):
    sim_event_id: int | None = None
    reason: str | None = None


class ChatBody(BaseModel):
    session_id: str = "default"
    message: str


class Runtime:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.engine = db.make_engine(s.DATABASE_URL, pool_size=5)
        self.breaker = CircuitBreaker("gemini", s.BREAKER_FAILURE_THRESHOLD, s.BREAKER_OPEN_SECONDS,
                                      timeout=s.GEMINI_TIMEOUT_MS / 1000)
        self.llm = GeminiLLM(s, self.breaker, log_call=self.ai_call)
        self.tools = ToolBox(self.engine, s.DECISION_URL, s.FORECAST_URL)
        self.copilot = Copilot(self.llm, self.tools)
        self.http = httpx.AsyncClient(timeout=5.0)
        self._explaining: set[str] = set()
        self._probe: asyncio.Task | None = None

    async def start(self) -> None:
        await db.wait_for_schema(self.engine, timeout_s=180)
        self._probe = asyncio.create_task(self.llm.probe())  # don't block startup on the network

    async def stop(self) -> None:
        if self._probe:
            self._probe.cancel()
        await self.tools.aclose()
        await self.http.aclose()
        await self.engine.dispose()

    async def ai_call(self, provider: str, purpose: str, latency_ms: int, ok: bool, error: str | None) -> None:
        with contextlib.suppress(Exception):
            async with self.engine.begin() as c:
                await c.execute(text("INSERT INTO ai_calls (provider, purpose, latency_ms, ok, error) "
                                     "VALUES (:p, :pu, :l, :ok, :e)"),
                                {"p": provider, "pu": purpose, "l": latency_ms, "ok": ok, "e": error})

    async def rows(self, sql: str, **p: Any) -> list[dict]:
        async with self.engine.connect() as c:
            return [dict(r) for r in (await c.execute(text(sql), p)).mappings().all()]


def create_app(runtime_factory=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        configure_logging(SERVICE, settings.LOG_LEVEL)
        rt = runtime_factory(settings) if runtime_factory else Runtime(settings)
        app.state.runtime = rt
        await rt.start()
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title="cognitive-svc", version="0.1.0", lifespan=lifespan)
    install_metrics(app, SERVICE)

    def rt() -> Runtime:
        return app.state.runtime

    @app.get("/health")
    async def health() -> dict[str, Any]:
        r: Runtime | None = getattr(app.state, "runtime", None)
        if r is None:
            return {"status": "starting"}
        ok = await db.ping(r.engine)
        return {"status": "ok" if ok else "degraded", "database": "ok" if ok else "down",
                "gemini": {"enabled": r.llm.enabled, "model": r.llm.model_name, "breaker": r.breaker.state.name}}

    @app.post("/explain")
    async def explain_ep(body: ExplainBody) -> dict[str, Any]:
        r = rt()
        rows = await r.rows("SELECT decision_id::text AS decision_id, facts, jev, explanation, cycle_tick "
                            "FROM decisions WHERE decision_id = CAST(:id AS uuid)", id=body.decision_id)
        if not rows:
            raise HTTPException(404, "decision not found")
        d = rows[0]
        if d["explanation"] and d["explanation"].get("source") == "GEMINI":
            return d["explanation"]
        tick = d["cycle_tick"]
        context = {
            "jev_triage": d["jev"],
            "events": await r.rows("SELECT id, type, status, start_tick, end_tick, parameters FROM sim_events "
                                   "WHERE status IN ('SCHEDULED','ACTIVE') ORDER BY id DESC LIMIT 10"),
            "recent_alerts": await r.rows("SELECT kind, severity, entity_id, fuel_type, message FROM alerts "
                                          "WHERE tick >= :t ORDER BY id DESC LIMIT 8", t=tick - 16),
        }
        out = await explain(d["facts"], context, r.llm)
        async with r.engine.begin() as c:
            await c.execute(text("UPDATE decisions SET explanation = CAST(:e AS jsonb), updated_at = now() "
                                 "WHERE decision_id = CAST(:id AS uuid)"),
                            {"e": json.dumps(out, default=str), "id": body.decision_id})
        return out

    @app.post("/incident")
    async def incident(body: IncidentBody) -> dict[str, Any]:
        r = rt()
        if body.sim_event_id is not None:
            existing = await r.rows("SELECT id, brief, source FROM incidents WHERE sim_event_id = :i", i=body.sim_event_id)
            if existing:
                return existing[0]
        events = await r.rows("SELECT id, type, status, start_tick, end_tick, parameters FROM sim_events "
                              "WHERE status IN ('ACTIVE','SCHEDULED') ORDER BY id DESC LIMIT 10")
        if body.sim_event_id is not None:
            events = [e for e in events if e["id"] == body.sim_event_id] + [e for e in events if e["id"] != body.sim_event_id and e["status"] == "ACTIVE"]
        at_risk: list[dict] = []
        try:
            fc = (await r.http.get(f"{r.s.FORECAST_URL}/forecast")).json()["forecasts"]
            at_risk = [{"station_id": f["station_id"], "fuel_type": f["fuel_type"],
                        "stockout_risk": round(f["stockout_risk"], 3), "t_empty_ticks": f["t_empty_ticks"]}
                       for f in fc if f["stockout_risk"] >= 0.2]
        except Exception:  # noqa: BLE001 - forecast-svc down: brief without risk figures
            pass
        tick = await r.rows("SELECT max(tick) AS tick FROM sim_ticks")
        facts = {"tick": tick[0]["tick"] if tick else None, "events": events, "at_risk": at_risk,
                 "reason": body.reason, "compound_crisis": sum(e["status"] == "ACTIVE" for e in events) >= 2,
                 "simulated_data": True}
        brief, source = await build_brief(facts, r.llm)
        async with r.engine.begin() as c:
            row = (await c.execute(text("INSERT INTO incidents (sim_event_id, brief, source) VALUES "
                                        "(:i, CAST(:b AS jsonb), :s) RETURNING id"),
                                   {"i": body.sim_event_id, "b": json.dumps({**brief, "facts": facts}, default=str),
                                    "s": source})).first()
        return {"id": row[0], "brief": brief, "source": source}

    @app.post("/copilot/chat")
    async def chat(body: ChatBody) -> dict[str, Any]:
        return await rt().copilot.chat(body.session_id, body.message)

    return app


app = create_app()
