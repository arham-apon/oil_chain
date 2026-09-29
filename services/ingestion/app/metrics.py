"""Prometheus metrics owned by ingestion-svc (names follow spec §15.1)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

SIM_TICK = Gauge("sim_tick", "Latest simulator tick synced")
SIM_SERVICE_LEVEL = Gauge("sim_service_level", "Simulator service level (served / (served + unmet))")
SIM_UNMET_LITERS = Gauge("sim_unmet_liters", "Cumulative unmet demand (liters)")
SIM_ALLOCATION_FAILURES = Gauge("sim_allocation_failures", "Simulator allocation failures")
SIM_STALE = Gauge("sim_stale", "1 while the simulator is serving stale data")
SIM_STATUS = Gauge("sim_status", "Simulator connectivity: 2 UP, 1 DEGRADED, 0 DOWN")
SSE_CONNECTED = Gauge("sse_connected", "1 while the SSE stream is connected")
SSE_RECONNECTS = Counter("sse_reconnects_total", "SSE reconnect attempts after a failure")
SYNC_LAG_TICKS = Gauge("sync_lag_ticks", "Latest tick seen minus last synced tick")
SYNC_DURATION = Histogram(
    "ingestion_sync_duration_seconds",
    "Duration of a sync run",
    ["kind"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
SYNCS = Counter("ingestion_syncs_total", "Sync runs", ["kind", "ok"])
PAYLOAD_INVALID = Counter(
    "sim_payload_invalid_total", "Simulator payloads rejected by validation", ["endpoint"]
)
ALERTS = Counter("alerts_total", "Alerts raised", ["kind"])
BUS_PUBLISH_ERRORS = Counter("bus_publish_errors_total", "Redis publish failures (ingestion keeps running)")
QUEUE_DROPPED = Counter("sse_events_dropped_total", "SSE events dropped because the local queue was full")
