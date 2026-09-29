"""Jev triage — System 1 (spec §11.6). Uses the documented ``typesafe-sdk`` (C7): ``AsyncTypeSafeClient().system_one``.

* one call per proposal, all three questions at once; ``Semaphore(8)``; breaker ``jev`` + ``JEV_TIMEOUT_MS``
* ``urgency_1to5 = score + 1`` (C6: Score is 0-indexed); Noul has no confidence (C8)
* results cached for 4 ticks per (station, fuel, rounded state)
* deterministic urgency is always computed and used when Jev is unavailable
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from fsp_shared.breaker import CircuitBreaker
from fsp_shared.logging import get_logger

from ..metrics import JEV_CALLS, JEV_LATENCY

log = get_logger("decision.jev")
CACHE_TICKS = 4

URGENCY_CRITERIA = [
    "Stock above 50% of capacity and time to empty above 24 ticks.",
    "Stock 30-50% of capacity, time to empty between 16 and 24 ticks.",
    "Approaching reorder point, time to empty between 8 and 16 ticks.",
    "High threat, time to empty between 4 and 8 ticks.",
    "Emergency, stockout expected in fewer than 4 ticks.",
]
CRISIS_CRITERIA = {
    "nominal": "Normal draw within expected bounds.",
    "transient_surge": "Consumption accelerated by a demand multiplier or spike.",
    "upstream_starvation": "Source depot reserves are too low to resupply.",
    "bottleneck_severed": "The transport route is disrupted or the station has only one route and it is unavailable.",
    "other": "Does not fit the other categories.",
}
AUTO_APPROVE_CRITERIA = {
    "true": "Quantity is within `station_available_ullage` and `route_max_shipment`, the route is AVAILABLE, "
    "and no unusual edge case is present.",
    "false": "Any bound is tight or violated, data is stale, or the situation is unusual and needs a human.",
}


@dataclass
class Triage:
    urgency_1to5: float
    urgency_conf: float | None = None
    urgency_probs: dict[str, float] | None = None
    crisis_class: str | None = None
    crisis_conf: float | None = None
    auto_approve: float | None = None  # Noul probability 0..1
    source: str = "DETERMINISTIC"  # JEV | JEV_CACHE | DETERMINISTIC
    deterministic_urgency: float = 1.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "urgency_1to5": self.urgency_1to5,
            "urgency_conf": self.urgency_conf,
            "urgency_probs": self.urgency_probs,
            "crisis_class": self.crisis_class,
            "crisis_conf": self.crisis_conf,
            "auto_approve": self.auto_approve,
            "source": self.source,
            "deterministic_urgency": self.deterministic_urgency,
        }


def deterministic_urgency(t_empty: int | None, stock_frac: float) -> float:
    """T_empty > 24 -> 1, 16-24 -> 2, 8-16 -> 3, 4-8 -> 4, < 4 -> 5 (stock % refines levels 1-2)."""
    if t_empty is not None:
        if t_empty < 4:
            return 5.0
        if t_empty < 8:
            return 4.0
        if t_empty < 16:
            return 3.0
        if t_empty <= 24:
            return 2.0
    if stock_frac < 0.3:
        return 3.0
    return 1.0 if stock_frac > 0.5 else 2.0


def _questions() -> dict[str, Any]:
    from typesafe_sdk import Choice, Noul, Score  # imported lazily: the service must start without the SDK working

    return {
        "urgency": Score(
            instructions="Score the inventory depletion urgency of `station` for `fuel_type` using "
            "`current_inventory_liters`, `tank_capacity_liters` and `time_to_empty_ticks`.",
            criteria=URGENCY_CRITERIA,
        ),
        "crisis_class": Choice(
            instructions="Which condition is the primary driver of stress at `station`? Use `active_events`, "
            "`demand_multiplier`, `depot_inventory_liters` and `route_status`.",
            criteria=CRISIS_CRITERIA,
        ),
        "auto_approve": Noul(
            instructions="Is `proposed_dispatch` safe to execute without human review?", criteria=AUTO_APPROVE_CRITERIA
        ),
    }


class JevTriage:
    def __init__(self, api_key: str, breaker: CircuitBreaker, timeout_ms: int, *, bad_key: bool = False,
                 log_call=None, model: str | None = None) -> None:
        self.breaker = breaker
        self.timeout_s = timeout_ms / 1000.0
        self.sem = asyncio.Semaphore(8)
        self.cache: dict[tuple, tuple[int, Triage]] = {}
        self.log_call = log_call  # async (provider, purpose, latency_ms, ok, error) -> None
        self.enabled = bool(api_key) or bad_key
        self._client = None
        if self.enabled:
            try:
                from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

                key = "invalid-chaos-key" if bad_key else api_key
                # installed SDK (verified): retry=RetryPolicy(max_retries=...) — disabled so the breaker sees failures
                self._client = AsyncTypeSafeClient(
                    api_key=key, model=model, retry=RetryPolicy(max_retries=0), timeout=timeout_ms / 1000.0
                )
            except Exception as exc:  # noqa: BLE001 - SDK missing/incompatible -> deterministic triage only
                log.warning("jev_unavailable", error=str(exc)[:200])
                self.enabled = False

    @staticmethod
    def _key(state: dict[str, Any], tick: int) -> tuple:
        return (
            state["station"], state["fuel_type"], round(state["current_inventory_liters"], -2),
            state["time_to_empty_ticks"], round(state["stockout_risk"], 1), state["proposed_dispatch"]["route_id"],
            round(state["proposed_dispatch"]["qty"], -2), state["data_stale"], tuple(sorted(state["active_events"])),
        )

    async def triage(self, state: dict[str, Any], tick: int) -> Triage:
        det = deterministic_urgency(state["time_to_empty_ticks"],
                                    state["current_inventory_liters"] / max(1.0, state["tank_capacity_liters"]))
        if not self.enabled or self._client is None:
            return Triage(urgency_1to5=det, deterministic_urgency=det)
        key = self._key(state, tick)
        hit = self.cache.get(key)
        if hit and tick - hit[0] < CACHE_TICKS:
            t = hit[1]
            return Triage(**{**t.__dict__, "source": "JEV_CACHE"})
        if not self.breaker.allow():
            self.breaker.record_fallback("breaker_open")
            return Triage(urgency_1to5=det, deterministic_urgency=det)
        async with self.sem:
            started = time.perf_counter()
            try:
                resp = await asyncio.wait_for(
                    self._client.system_one(state=state, questions=_questions()), self.timeout_s
                )
                a = resp.answers
                u, c, n = a["urgency"], a["crisis_class"], a["auto_approve"]
                result = Triage(
                    urgency_1to5=float(u.score) + 1.0,  # C6
                    urgency_conf=getattr(u, "confidence", None),
                    urgency_probs=dict(getattr(u, "probabilities", {}) or {}),
                    crisis_class=getattr(c, "choice", None),
                    crisis_conf=getattr(c, "confidence", None),
                    auto_approve=float(n.noul),  # C8: Noul has no confidence
                    source="JEV",
                    deterministic_urgency=det,
                )
                self.breaker.record_success()
                ok, err = True, None
            except Exception as exc:  # noqa: BLE001 - 401/429/529/timeouts all -> deterministic
                self.breaker.record_failure()
                self.breaker.record_fallback(type(exc).__name__)
                result = Triage(urgency_1to5=det, deterministic_urgency=det)
                ok, err = False, f"{type(exc).__name__}: {str(exc)[:200]}"
            latency = time.perf_counter() - started
        JEV_LATENCY.observe(latency)
        JEV_CALLS.labels(str(ok).lower()).inc()
        if self.log_call:
            await self.log_call("typesafe-jev", "triage", int(latency * 1000), ok, err)
        if ok:
            self.cache[key] = (tick, result)
        return result
