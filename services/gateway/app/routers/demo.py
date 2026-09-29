"""Optional demo controls (spec §13.1): proxy to the simulator's ``/admin/*`` endpoints, only when DEMO_CONTROLS=true.

The production decision path never uses these; they exist so the live demo can be driven from the UI. Every call is
audited with the operator's name."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fsp_shared.exceptions import SimAPIError, SimPayloadError, SimTransportError
from pydantic import BaseModel, Field

from ..runtime import Runtime
from .decisions import operator

router = APIRouter(prefix="/api/demo", tags=["demo"])

EventType = Literal["demand_spike", "route_disruption", "station_outage", "depot_constraint", "shipment_delay",
                    "supply_shortfall"]
FaultType = Literal["latency", "unavailable", "error_rate", "stale_data", "stream_disconnect"]


class EventBody(BaseModel):
    type: EventType
    start_tick: int | None = Field(default=None, ge=0, description="default: current tick + 2")
    duration_ticks: int = Field(default=24, ge=1, le=2000)
    parameters: dict[str, Any] = Field(default_factory=dict)


class FaultBody(BaseModel):
    type: FaultType
    duration_seconds: int = Field(default=60, ge=1, le=3600)
    parameters: dict[str, Any] = Field(default_factory=dict)


def _rt(request: Request) -> Runtime:
    rt: Runtime = request.app.state.runtime
    if not rt.s.DEMO_CONTROLS:
        raise HTTPException(404, "demo controls are disabled (DEMO_CONTROLS=false)")
    return rt


async def _admin(request: Request, action: str, call, data: dict | None = None) -> Any:
    rt = _rt(request)
    try:
        out = await call(rt)
    except SimAPIError as exc:
        await rt.audit(actor=operator(request), action=f"demo.{action}", entity_type="simulator", entity_id=None,
                       result=f"ERROR {exc.error.code}", data=data)
        raise HTTPException(exc.error.status or 502, {"code": exc.error.code, "message": exc.error.message}) from exc
    except (SimTransportError, SimPayloadError) as exc:
        await rt.audit(actor=operator(request), action=f"demo.{action}", entity_type="simulator", entity_id=None,
                       result="ERROR unreachable", data=data)
        raise HTTPException(502, f"simulator unreachable: {exc}") from exc
    await rt.audit(actor=operator(request), action=f"demo.{action}", entity_type="simulator", entity_id=None,
                   result="OK", data=data)
    return out


@router.post("/events")
async def create_event(body: EventBody, request: Request) -> Any:
    rt = _rt(request)
    start = body.start_tick
    if start is None:
        row = await rt.one("SELECT max(tick) AS t FROM sim_ticks")
        start = int((row or {}).get("t") or 0) + 2
    data = {**body.model_dump(), "start_tick": start}
    return await _admin(request, "event", lambda r: r.sim.admin_create_event(body.type, start, body.duration_ticks,
                                                                            body.parameters), data)


@router.post("/faults")
async def create_fault(body: FaultBody, request: Request) -> Any:
    return await _admin(request, "fault", lambda r: r.sim.admin_create_fault(body.type, body.duration_seconds,
                                                                             body.parameters), body.model_dump())


@router.post("/faults/clear")
async def clear_faults(request: Request) -> Any:
    return await _admin(request, "faults_clear", lambda r: r.sim.admin_clear_faults())


@router.get("/faults")
async def faults(request: Request) -> Any:
    rt = _rt(request)
    try:
        return await rt.sim.admin_faults()
    except (SimAPIError, SimTransportError, SimPayloadError) as exc:
        raise HTTPException(502, f"simulator unreachable: {exc}") from exc


# registered last: the catch-all must not shadow /events and /faults
@router.post("/{action}")
async def control(action: Literal["run", "pause", "step", "reset", "toggle"], request: Request) -> Any:
    fns = {"run": lambda rt: rt.sim.admin_run(), "pause": lambda rt: rt.sim.admin_pause(),
           "step": lambda rt: rt.sim.admin_step(), "reset": lambda rt: rt.sim.admin_reset(),
           "toggle": lambda rt: rt.sim.admin_toggle()}
    return await _admin(request, action, fns[action])
