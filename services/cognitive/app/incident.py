"""Incident briefs (spec §12.3): built when a sim event becomes ACTIVE (or a compound crisis), stored in ``incidents``."""

from __future__ import annotations

import json
from typing import Any

from fsp_shared import world_constants as wc
from langchain_core.messages import HumanMessage, SystemMessage

from . import grounding
from .llm import GeminiLLM, LLMUnavailable
from .metrics import INCIDENTS
from .schemas import IncidentBrief

SYSTEM = (
    "You write short incident briefs for operators of a SIMULATED fuel supply network. Use ONLY the entities and numbers "
    "in FACTS. Remember: station-tongi is reachable only via route-gazipur-tongi and station-coxsbazar only via "
    "route-patiya-coxsbazar (no alternate path); station-mirpur and station-karnaphuli each have two routes. "
    "Recommend concrete operator actions (pre-stock, reroute, prioritize, watch depot reserves)."
)


def affected(event: dict[str, Any]) -> list[str]:
    p = event.get("parameters") or {}
    ids = list(p.get("station_ids") or []) + list(p.get("route_ids") or []) + list(p.get("depot_ids") or []) \
        + list(p.get("region_ids") or [])
    if not ids:
        ids = {"route_disruption": list(wc.ROUTES), "depot_constraint": list(wc.DEPOTS),
               "shipment_delay": list(wc.DEPOTS), "supply_shortfall": list(wc.DEPOTS)}.get(event.get("type", ""),
                                                                                             list(wc.STATIONS))
    return ids


def template_brief(facts: dict[str, Any]) -> dict[str, Any]:
    events = facts.get("events", [])
    titles = ", ".join(f"{e['type']} (#{e['id']})" for e in events) or "Operational incident"
    ents = sorted({x for e in events for x in affected(e)})
    actions = []
    for e in events:
        t = e["type"]
        if t == "route_disruption":
            for rid in affected(e):
                st = wc.ROUTES.get(rid, {}).get("station")
                if st in wc.SINGLE_ROUTE_STATIONS:
                    actions.append(f"{st} has no alternate path while {rid} is down: pre-stock before and prioritize "
                                   "refill when it returns.")
                elif st:
                    actions.append(f"Reroute {st} via its alternate route; cancel PENDING allocations on {rid}.")
        elif t == "demand_spike":
            actions.append("Raise replenishment for the spiking stations; watch time-to-empty closely.")
        elif t == "station_outage":
            actions.append("Hold dispatches to stations in OUTAGE; plan catch-up deliveries for when they reopen.")
        elif t == "depot_constraint":
            actions.append("Depot throughput reduced: shift volume to the other depot's cross-region routes.")
        elif t in ("shipment_delay", "supply_shortfall"):
            actions.append("Protect depot reserves for own-region stations until the delayed/short supply lands.")
    return {
        "title": titles,
        "affected_entities": ents,
        "impact_summary": f"{len(events)} active event(s); {len(facts.get('at_risk', []))} station/fuel pair(s) at "
                          "elevated stockout risk.",
        "expected_duration_ticks": max((e["end_tick"] - e["start_tick"] for e in events), default=None),
        "recommended_operator_actions": actions or ["Monitor the Decision Center for staged proposals."],
        "lifelines_at_risk": [f"{a['station_id']}/{a['fuel_type']}" for a in facts.get("at_risk", [])],
    }


async def build_brief(facts: dict[str, Any], llm: GeminiLLM) -> tuple[dict[str, Any], str]:
    try:
        out = await llm.call(llm.llm(structured=IncidentBrief),
                             [SystemMessage(content=SYSTEM), HumanMessage(content="FACTS:\n" + json.dumps(facts, default=str))],
                             "incident")
        if isinstance(out, IncidentBrief) and not grounding.check(grounding.narrative_text(out), facts):
            INCIDENTS.labels("GEMINI").inc()
            return out.model_dump(), "GEMINI"
    except LLMUnavailable:
        pass
    INCIDENTS.labels("TEMPLATE").inc()
    return template_brief(facts), "TEMPLATE"
