"""Gating policy (spec §11.7): decides AUTO_APPROVED vs STAGED_REVIEW and the system origin."""

from __future__ import annotations

from dataclasses import dataclass

from fsp_shared.breaker import BreakerState

from .jev import Triage

LOW_RISK = 0.5


@dataclass(frozen=True)
class Gate:
    status: str  # AUTO_APPROVED | STAGED_REVIEW
    origin: str  # SYSTEM1_AUTO | HEURISTIC_FALLBACK
    reason: str


def gate(
    triage: Triage,
    *,
    data_stale: bool,
    jev_breaker: BreakerState,
    jev_enabled: bool,
    forecast_source: str,
    planner: str,
    stockout_risk: float,
    noul_min: float,
    urgency_max: float,
) -> Gate:
    fallback = forecast_source == "BASELINE_FALLBACK" or planner == "HEURISTIC"
    origin_auto = "HEURISTIC_FALLBACK" if fallback else "SYSTEM1_AUTO"
    if data_stale:
        return Gate("STAGED_REVIEW", origin_auto, "stale simulator data: autonomous dispatch suspended")
    if not jev_enabled or jev_breaker is BreakerState.OPEN or triage.source == "DETERMINISTIC":
        # cloud AI down: deterministic checks already passed, dispatch continues (brief: operations continue)
        return Gate("AUTO_APPROVED", "HEURISTIC_FALLBACK", "Jev unavailable: deterministic gate")
    if forecast_source == "BASELINE_FALLBACK" and stockout_risk < LOW_RISK:
        return Gate("AUTO_APPROVED", "HEURISTIC_FALLBACK", "forecast fallback, low risk")
    if (
        triage.auto_approve is not None
        and triage.auto_approve >= noul_min
        and triage.urgency_1to5 < urgency_max
        and (triage.urgency_conf or 0.0) >= 0.4
    ):
        return Gate("AUTO_APPROVED", origin_auto, "Jev: safe, not critical, confident")
    return Gate("STAGED_REVIEW", origin_auto, "low confidence or critical: needs operator review")
