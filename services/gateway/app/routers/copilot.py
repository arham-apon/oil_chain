"""Operator copilot proxy (cognitive-svc). The copilot's tools are read-only; ``propose_allocation`` only stages."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..runtime import Runtime

router = APIRouter(prefix="/api", tags=["copilot"])


class ChatBody(BaseModel):
    session_id: str = Field(default="default", max_length=80)
    message: str = Field(min_length=1, max_length=2000)


@router.post("/copilot/chat")
async def chat(body: ChatBody, request: Request) -> dict[str, Any]:
    rt: Runtime = request.app.state.runtime
    up = await rt.call("POST", f"{rt.s.COGNITIVE_URL}/copilot/chat", json=body.model_dump(), timeout=25.0)
    if up.ok:
        return up.data
    return {"answer": "The copilot service is unreachable right now. Dispatching, forecasts and the Decision Center "
                      "keep working without it.", "tool_calls": [], "source": "UNAVAILABLE", "error": up.error,
            "simulated_data": True}
