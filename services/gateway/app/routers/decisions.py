"""Decision Center endpoints: proxy to decision-svc (approve / reject / manual / what-if / dry-run cycle) and
cognitive-svc (on-demand explanation). The operator identity comes from the ``X-Operator`` header."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..runtime import Runtime, Upstream

router = APIRouter(prefix="/api", tags=["decisions"])


class ApproveBody(BaseModel):
    qty: float | None = Field(default=None, gt=0)
    route_id: str | None = None
    note: str | None = None


class RejectBody(BaseModel):
    note: str | None = None


class ManualBody(BaseModel):
    station_id: str
    fuel_type: str
    qty: float = Field(gt=0)
    route_id: str
    note: str | None = None
    staged: bool = True  # operator-created proposals go through the same approval step by default


def _rt(request: Request) -> Runtime:
    return request.app.state.runtime


def operator(request: Request) -> str:
    return (request.headers.get("x-operator") or "operator").strip()[:60] or "operator"


def _raise(up: Upstream) -> Any:
    if up.ok:
        return up.data
    if up.status:
        detail = up.data.get("detail") if isinstance(up.data, dict) else up.error
        raise HTTPException(up.status, detail)
    raise HTTPException(503, f"decision-svc unavailable ({up.error})")


@router.get("/decisions")
async def decisions(request: Request, status: str | None = None,
                    limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    rt = _rt(request)
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    return _raise(await rt.call("GET", f"{rt.s.DECISION_URL}/decisions", params=params, timeout=3.0))


@router.get("/decisions/{decision_id}")
async def decision(decision_id: str, request: Request) -> dict[str, Any]:
    rt = _rt(request)
    return _raise(await rt.call("GET", f"{rt.s.DECISION_URL}/decisions/{decision_id}", timeout=3.0))


@router.post("/decisions/{decision_id}/approve")
async def approve(decision_id: str, body: ApproveBody, request: Request) -> dict[str, Any]:
    rt = _rt(request)
    op = operator(request)
    up = await rt.call("POST", f"{rt.s.DECISION_URL}/decisions/{decision_id}/approve", timeout=15.0,
                       json={**body.model_dump(), "operator": op})
    return _raise(up)  # decision-svc audits approvals itself


@router.post("/decisions/{decision_id}/reject")
async def reject(decision_id: str, body: RejectBody, request: Request) -> dict[str, Any]:
    rt = _rt(request)
    up = await rt.call("POST", f"{rt.s.DECISION_URL}/decisions/{decision_id}/reject", timeout=5.0,
                       json={"note": body.note, "operator": operator(request)})
    return _raise(up)


@router.post("/decisions/manual")
async def manual(body: ManualBody, request: Request) -> dict[str, Any]:
    rt = _rt(request)
    up = await rt.call("POST", f"{rt.s.DECISION_URL}/decisions/manual", timeout=15.0,
                       json={**body.model_dump(), "operator": operator(request), "origin": "OPERATOR_MANUAL"})
    return _raise(up)


@router.post("/decisions/{decision_id}/explain")
async def explain(decision_id: str, request: Request) -> dict[str, Any]:
    rt = _rt(request)
    up = await rt.call("POST", f"{rt.s.COGNITIVE_URL}/explain", json={"decision_id": decision_id}, timeout=15.0)
    if not up.ok and not up.status:
        raise HTTPException(503, f"cognitive-svc unavailable ({up.error}); the facts table is still valid")
    return _raise(up)


@router.post("/whatif")
async def whatif(request: Request) -> dict[str, Any]:
    rt = _rt(request)
    body = await request.json()
    return _raise(await rt.call("POST", f"{rt.s.DECISION_URL}/whatif", json=body, timeout=5.0))


@router.post("/cycle/run")
async def run_cycle(request: Request, dry_run: bool = True) -> dict[str, Any]:
    rt = _rt(request)
    up = await rt.call("POST", f"{rt.s.DECISION_URL}/cycle/run", params={"dry_run": str(dry_run).lower()},
                       timeout=20.0)
    if not dry_run:
        await rt.audit(actor=operator(request), action="decision.cycle_run", entity_type="cycle", entity_id=None,
                       result="OK" if up.ok else "ERROR", data={"dry_run": dry_run})
    return _raise(up)


@router.get("/decision/status")
async def decision_status(request: Request) -> dict[str, Any]:
    return _raise(await _rt(request).decision_status())
