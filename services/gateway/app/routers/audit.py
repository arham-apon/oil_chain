"""Audit log and AI-call log (spec §15.4). The allocation ledger is ``GET /api/allocations``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from ..runtime import Runtime

router = APIRouter(prefix="/api", tags=["audit"])


@router.get("/audit")
async def audit(request: Request, limit: int = Query(200, ge=1, le=1000), action: str | None = None,
                actor: str | None = None) -> list[dict[str, Any]]:
    rt: Runtime = request.app.state.runtime
    where, params = [], {"n": limit}
    if action:
        where.append("action LIKE :a")
        params["a"] = f"{action}%"
    if actor:
        where.append("actor = :actor")
        params["actor"] = actor
    q = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "")
    return await rt.rows(q + " ORDER BY id DESC LIMIT :n", **params)


@router.get("/ai-calls")
async def ai_calls(request: Request, limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    rt: Runtime = request.app.state.runtime
    rows = await rt.rows("SELECT * FROM ai_calls ORDER BY id DESC LIMIT :n", n=limit)
    summary = await rt.rows(
        "SELECT provider, purpose, count(*) AS calls, sum(CASE WHEN ok THEN 1 ELSE 0 END) AS ok, "
        "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms FROM ai_calls "
        "WHERE at > now() - interval '1 hour' GROUP BY 1, 2 ORDER BY 1, 2")
    return {"calls": rows, "last_hour": summary}
