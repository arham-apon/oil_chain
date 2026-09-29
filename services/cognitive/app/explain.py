"""Decision explanations: computed FACTS + Gemini narrative, with number-grounding post-validation and a
template fallback (spec §12.2). The UI always renders the facts table; the narrative is additive."""

from __future__ import annotations

import json
from typing import Any

from fsp_shared.logging import get_logger
from langchain_core.messages import HumanMessage, SystemMessage

from . import grounding
from .llm import GeminiLLM, LLMUnavailable
from .metrics import EXPLANATIONS
from .schemas import DispatchNarrative

log = get_logger("cognitive.explain")
# explanations are requested asynchronously (never on the dispatch path), so they may take longer than
# GEMINI_TIMEOUT_MS; the breaker still counts timeouts as failures (DECISIONS D27)
ASYNC_TIMEOUT_S = 30.0

SYSTEM = (
    "You are the System 2 diagnostic engine of a SIMULATED fuel supply operations platform (training data, not real "
    "infrastructure). Explain a proposed fuel dispatch to a human operator. Use ONLY numbers present in FACTS. Never "
    "invent quantities, routes, depots, or probabilities. Analyze root causes. Quote stockout_risk_before and "
    "stockout_risk_after exactly as given in FACTS. If FACTS.alternatives has no feasible entry, say no alternative "
    "exists. Keep every field short."
)


def _pct(x: Any) -> str:
    return "n/a" if x is None else f"{float(x) * 100:.1f}%"


def template_narrative(facts: dict[str, Any]) -> dict[str, Any]:
    f = facts
    hours = f.get("projected_stockout_hours")
    causes = []
    if hours is not None:
        causes.append(f"{f['alert_entity_id']} {f['fuel_type']} projected to run empty in {hours} h at "
                      f"{f.get('burn_rate_liters_per_hour')} L/h.")
    else:
        causes.append(f"Keeps {f['alert_entity_id']} {f['fuel_type']} above its target level over the planning horizon.")
    if f.get("single_route_station"):
        causes.append(f"{f['alert_entity_id']} is reachable by a single route; there is no alternate path.")
    alts = [a for a in f.get("alternatives", []) if a.get("feasible_qty")]
    return {
        "summary": (f"Send {f['recommended_allocation_liters']} L of {f['fuel_type']} from {f['source_depot_id']} to "
                    f"{f['alert_entity_id']} via {f['transit_route_id']} (arrives in {f.get('transit_ticks')} ticks + 1)."),
        "primary_causal_factors": causes,
        "risk_mitigation_delta": (f"Stockout risk {_pct(f.get('stockout_risk_before'))} -> "
                                  f"{_pct(f.get('stockout_risk_after'))} "
                                  f"(stockout_risk_before {f.get('stockout_risk_before')}, "
                                  f"stockout_risk_after {f.get('stockout_risk_after')})."),
        "binding_constraints_explained": [
            {"route_max_shipment": "The route's maximum shipment size caps this allocation.",
             "depot_stock": "Usable depot stock for this fuel is exhausted by the plan.",
             "depot_dispatch_capacity": "The depot's per-tick dispatch capacity is fully used.",
             "station_ullage": "The station tank (incl. fuel already in flight) would be at 98% capacity.",
             "need_not_fully_covered": "Not all of the station's need could be covered this cycle.",
             "depot_reserve_shortfall": "The source depot dips below the reserve kept for its own region."}.get(b, b)
            for b in f.get("binding_constraints", [])
        ],
        "fallback_contingency": (
            f"Alternative: {alts[0]['feasible_qty']} L via {alts[0]['route_id']} (risk after "
            f"{_pct(alts[0].get('stockout_risk_after'))})." if alts else "No feasible alternative route exists."
        ),
        "operator_checks": [
            "Confirm the route is still AVAILABLE and no disruption is scheduled at departure.",
            "Confirm tank ullage including in-flight deliveries.",
            "Check the source depot's next supply arrival before dispatching large volumes.",
        ],
    }


async def explain(facts: dict[str, Any], context: dict[str, Any], llm: GeminiLLM) -> dict[str, Any]:
    """Returns {"source": GEMINI|TEMPLATE, "narrative": {...}, "validation": [...], "model": ...}."""
    try:
        runnable = llm.llm(structured=DispatchNarrative, timeout=ASYNC_TIMEOUT_S)
        msgs = [
            SystemMessage(content=SYSTEM),
            HumanMessage(content="FACTS:\n" + json.dumps(facts, default=str) + "\n\nCONTEXT (events, Jev triage, "
                         "recent alerts):\n" + json.dumps(context, default=str)),
        ]
        out: DispatchNarrative = await llm.call(runnable, msgs, "explain", timeout=ASYNC_TIMEOUT_S)
        problems = grounding.check(grounding.narrative_text(out), facts, context)
        if isinstance(out, DispatchNarrative) and not problems:
            EXPLANATIONS.labels(llm.label).inc()
            return {"source": llm.label, "model": llm.model_name, "narrative": out.model_dump(), "validation": []}
        log.warning("narrative_rejected", problems=problems[:5])
        EXPLANATIONS.labels("TEMPLATE").inc()
        return {"source": "TEMPLATE", "narrative": template_narrative(facts), "validation": problems,
                "rejected_llm_output": out.model_dump() if hasattr(out, "model_dump") else None}
    except LLMUnavailable as exc:
        EXPLANATIONS.labels("TEMPLATE").inc()
        return {"source": "TEMPLATE", "narrative": template_narrative(facts), "validation": [],
                "llm_unavailable": str(exc)[:200]}
