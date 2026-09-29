"""Structured outputs Gemini must fill (spec §12.2 / §12.3)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DispatchNarrative(BaseModel):
    summary: str = Field(description="1-2 sentences for the operator")
    primary_causal_factors: list[str] = Field(description="root causes, grounded in the FACTS")
    risk_mitigation_delta: str = Field(description="must quote stockout_risk_before and stockout_risk_after from FACTS")
    binding_constraints_explained: list[str] = Field(description="plain-language meaning of each binding constraint")
    fallback_contingency: str = Field(description="reference an entry of FACTS.alternatives, or say none exists")
    operator_checks: list[str] = Field(description="what the operator should verify before approving")


class IncidentBrief(BaseModel):
    title: str
    affected_entities: list[str]
    impact_summary: str
    expected_duration_ticks: int | None = None
    recommended_operator_actions: list[str]
    lifelines_at_risk: list[str] = Field(description="station/fuel pairs at risk of stockout because of this incident")
