"""Prometheus metrics owned by decision-svc (spec §11 / §15.1)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

DECISION_CYCLE = Histogram(
    "decision_cycle_seconds", "Decision cycle duration", buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10)
)
CYCLES_SKIPPED = Counter("decision_cycles_skipped_total", "Cycle triggers skipped (single-flight)")
DECISIONS = Counter("decisions_total", "Decisions created", ["origin", "status"])
ALLOC_COMMITTED = Counter("allocations_committed_total", "Allocations committed to the simulator", ["origin"])
ALLOC_REJECTED = Counter("allocation_rejections_total", "Allocation rejections", ["code"])
EXECUTOR_LATENCY = Histogram("executor_post_latency_seconds", "POST /v1/allocations latency")
OVERRIDES = Counter("operator_overrides_total", "Operator approvals with edits")
ALERTS = Counter("alerts_total", "Alerts raised", ["kind"])
STOCKOUT_RISK = Gauge("stockout_risk", "Stockout risk within horizon", ["station", "fuel"])
JEV_LATENCY = Histogram("jev_latency_seconds", "Jev call latency", buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 1, 2))
JEV_CALLS = Counter("jev_calls_total", "Jev calls", ["ok"])
CANCELLATIONS = Counter("allocations_cancelled_total", "PENDING allocations cancelled", ["result"])
MODE = Gauge("decision_mode", "0 NORMAL, 1 DEGRADED, 2 FALLBACK")
PLANNER_USED = Counter("planner_runs_total", "Planner runs", ["planner"])
