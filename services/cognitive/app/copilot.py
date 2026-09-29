"""Operator copilot: LangChain tool-calling loop (``bind_tools``), max 6 tool steps, 20 s per turn (spec §12.4)."""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any

from fsp_shared.logging import get_logger
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from .llm import GeminiLLM, LLMUnavailable
from .metrics import COPILOT_TURNS
from .tools import ToolBox

log = get_logger("cognitive.copilot")
MAX_STEPS = 6
TURN_TIMEOUT_S = 20.0
MAX_SESSIONS = 200

SYSTEM = (
    "You are the operator copilot of a SIMULATED fuel supply operations platform (BUP hackathon simulator; no real "
    "infrastructure). Answer ONLY from tool outputs; if data is missing, say so. Never invent numbers. Routes are direct "
    "depot->station only: station-tongi (only route-gazipur-tongi) and station-coxsbazar (only route-patiya-coxsbazar) "
    "have no alternate path; station-mirpur (route-gazipur-mirpur 2 ticks, route-patiya-mirpur 4 ticks) and "
    "station-karnaphuli (route-patiya-karnaphuli 2, route-gazipur-karnaphuli 4) can be rerouted. For what-if questions "
    "call simulate_allocation (it supports overrides such as depot_derate={'depot-gazipur': 0.5}) and quote the "
    "before/after stockout risks it returns. You may propose_allocation, which only stages a proposal for human "
    "approval; you can never dispatch fuel. Be concise and end with the key numbers. 1 tick = 15 simulated minutes."
)


class Copilot:
    def __init__(self, llm: GeminiLLM, tools: ToolBox) -> None:
        self.llm = llm
        self.tools = tools
        self.sessions: OrderedDict[str, list[BaseMessage]] = OrderedDict()

    async def chat(self, session_id: str, message: str) -> dict[str, Any]:
        trace: list[dict[str, Any]] = []
        history = self.sessions.setdefault(session_id, [])
        self.sessions.move_to_end(session_id)
        while len(self.sessions) > MAX_SESSIONS:
            self.sessions.popitem(last=False)
        try:
            answer = await asyncio.wait_for(self._run(history, message, trace), TURN_TIMEOUT_S)
            COPILOT_TURNS.labels("GEMINI").inc()
            return {"answer": answer, "tool_calls": trace, "source": "GEMINI", "simulated_data": True}
        except (LLMUnavailable, TimeoutError) as exc:
            COPILOT_TURNS.labels("UNAVAILABLE").inc()
            snapshot = None
            try:
                snapshot = await self.tools.get_network_state()
            except Exception:  # noqa: BLE001
                pass
            return {
                "answer": "The AI copilot (Gemini) is currently unavailable, so I can't reason about this question. "
                          "The Decision Center, forecasts and what-if simulation keep working without it; the raw "
                          "network state is attached.",
                "tool_calls": trace, "source": "UNAVAILABLE", "error": str(exc)[:200], "network_state": snapshot,
                "simulated_data": True,
            }

    async def _run(self, history: list[BaseMessage], message: str, trace: list[dict]) -> str:
        lc_tools = self.tools.langchain_tools()
        by_name = {t.name: t for t in lc_tools}
        model = self.llm.llm(tools=lc_tools, timeout=15.0)
        msgs: list[BaseMessage] = [SystemMessage(content=SYSTEM), *history[-10:], HumanMessage(content=message)]
        for _ in range(MAX_STEPS + 1):
            ai: AIMessage = await self.llm.call(model, msgs, "copilot", timeout=15.0)
            msgs.append(ai)
            calls = getattr(ai, "tool_calls", None) or []
            if not calls or len(trace) >= MAX_STEPS:
                text = ai.content if isinstance(ai.content, str) else " ".join(
                    p.get("text", "") for p in ai.content if isinstance(p, dict))
                history.extend([HumanMessage(content=message), AIMessage(content=text)])
                return text or "(no answer)"
            for call in calls:
                started = time.perf_counter()
                tool = by_name.get(call["name"])
                try:
                    result = await tool.ainvoke(call["args"]) if tool else {"error": f"unknown tool {call['name']}"}
                    ok = True
                except Exception as exc:  # noqa: BLE001 - tool errors go back to the model
                    result, ok = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, False
                content = json.dumps(result, default=str)[:12000]
                trace.append({"tool": call["name"], "args": call["args"], "ok": ok,
                              "ms": round((time.perf_counter() - started) * 1000, 1), "result_preview": content[:600]})
                msgs.append(ToolMessage(content=content, tool_call_id=call.get("id") or call["name"]))
        return "Stopped after the maximum number of tool steps."
