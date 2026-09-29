from app import grounding
from app.explain import template_narrative
from app.incident import template_brief

FACTS = {
    "alert_entity_id": "station-tongi", "fuel_type": "DIESEL", "projected_stockout_hours": 3.5,
    "burn_rate_liters_per_hour": 580.2, "recommended_allocation_liters": 6500, "source_depot_id": "depot-gazipur",
    "transit_route_id": "route-gazipur-tongi", "transit_ticks": 2, "stockout_risk_before": 0.72,
    "stockout_risk_after": 0.19, "binding_constraints": ["route_max_shipment"], "single_route_station": True,
    "alternatives": [{"route_id": None, "note": "no alternate path"}],
}


def test_grounded_text_passes_and_percent_forms_are_accepted():
    ok = "Send 6,500 L via route-gazipur-tongi from depot-gazipur; risk falls from 72% to 19% (0.72 -> 0.19) in 3.5 h."
    assert grounding.check(ok, FACTS) == []


def test_invented_numbers_and_ids_are_rejected():
    bad = "Send 9000 L via route-gazipur-coxsbazar; risk drops to 5%."
    problems = grounding.check(bad, FACTS)
    assert any("9000" in p for p in problems) and any("route-gazipur-coxsbazar" in p for p in problems)
    assert any("5%" in p for p in problems)


def test_template_narrative_is_always_grounded():
    n = template_narrative(FACTS)
    assert grounding.check(grounding.narrative_text(n), FACTS) == []
    assert "No feasible alternative" in n["fallback_contingency"] and "single route" in " ".join(n["primary_causal_factors"])


def test_incident_template_flags_single_route_station():
    facts = {"events": [{"id": 3, "type": "route_disruption", "status": "ACTIVE", "start_tick": 10, "end_tick": 30,
                         "parameters": {"route_ids": ["route-gazipur-tongi", "route-gazipur-mirpur"]}}], "at_risk": []}
    b = template_brief(facts)
    text = " ".join(b["recommended_operator_actions"])
    assert "station-tongi has no alternate path" in text and "Reroute station-mirpur" in text
    assert b["expected_duration_ticks"] == 20
