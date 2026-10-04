"""Generate the provisioned Grafana dashboards (run: python infra/grafana/build_dashboards.py)."""

import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}

P95 = 'histogram_quantile(0.95, sum by (le, service) (rate(http_request_duration_seconds_bucket[1m])))'


def panel(i, title, exprs, kind="timeseries", unit=None, w=12, h=8, legend="{{service}}"):
    p = {
        "id": i, "title": title, "type": kind, "datasource": DS,
        "gridPos": {"x": ((i - 1) % (24 // w)) * w, "y": ((i - 1) // (24 // w)) * h, "w": w, "h": h},
        "targets": [{"refId": chr(65 + n), "expr": e, "legendFormat": legend if isinstance(e, str) else ""}
                    for n, e in enumerate(exprs)],
        "fieldConfig": {"defaults": {"unit": unit} if unit else {}, "overrides": []},
    }
    return p


def dash(uid, title, panels):
    return {"uid": uid, "title": title, "schemaVersion": 39, "refresh": "5s", "time": {"from": "now-15m", "to": "now"},
            "tags": ["fsp", "simulated"], "panels": [panel(i + 1, *p[:2], **p[2]) for i, p in enumerate(panels)]}


DASHBOARDS = [
    dash("fsp-overview", "Platform Overview", [
        ("Service up", ['up{job="fsp-services"}'], {"kind": "stat"}),
        ("Request rate (req/s)", ['sum by (service) (rate(http_requests_total[1m]))'], {"unit": "reqps"}),
        ("p95 latency", [P95], {"unit": "s"}),
        ("5xx error rate %", ['100 * sum by (service) (rate(http_requests_total{status=~"5.."}[1m])) / '
                              'clamp_min(sum by (service) (rate(http_requests_total[1m])), 1e-9)'], {"unit": "percent"}),
        ("Service level (simulated)", ["sim_service_level"], {"kind": "stat", "unit": "percentunit", "legend": "SL"}),
        ("Uptime", ['time() - service_start_time_seconds'], {"unit": "s"}),
    ]),
    dash("fsp-simulator", "Simulator Integration", [
        ("Simulator tick", ["sim_tick"], {"legend": "tick"}),
        ("SSE connected / reconnects", ["sse_connected", "sse_reconnects_total"], {"legend": "{{__name__}}"}),
        ("Sync lag (ticks)", ["sync_lag_ticks"], {"legend": "lag"}),
        ("Faults detected", ["sum by (type) (increase(sim_faults_detected_total[1m]))"], {"legend": "{{type}}"}),
        ("Simulator client p95 by endpoint",
         ['histogram_quantile(0.95, sum by (le, endpoint) (rate(sim_client_latency_seconds_bucket[1m])))'],
         {"unit": "s", "legend": "{{endpoint}}"}),
        ("Unmet liters / allocation failures", ["sim_unmet_liters", "sim_allocation_failures"], {"legend": "{{__name__}}"}),
        ("Stale data flag", ["sim_stale"], {"kind": "stat", "legend": "stale"}),
        ("Simulator status (2 UP,1 DEGRADED,0 DOWN)", ["sim_status"], {"kind": "stat", "legend": "status"}),
    ]),
    dash("fsp-intelligence", "Intelligence & Decisions", [
        ("Decisions by origin/status", ["sum by (origin, status) (increase(decisions_total[5m]))"], {"legend": "{{origin}} {{status}}"}),
        ("Allocations committed", ["sum by (origin) (increase(allocations_committed_total[5m]))"], {"legend": "{{origin}}"}),
        ("Allocation rejections by code", ["sum by (code) (increase(allocation_rejections_total[5m]))"], {"legend": "{{code}}"}),
        ("Breaker state (0 closed, 1 half, 2 open)", ["breaker_state"], {"legend": "{{dependency}}"}),
        ("Fallback activations", ["sum by (dependency, reason) (increase(fallback_activations_total[5m]))"], {"legend": "{{dependency}} {{reason}}"}),
        ("Stockout risk", ["stockout_risk"], {"unit": "percentunit", "legend": "{{station}} {{fuel}}"}),
        ("Forecast MAE (L/tick)", ["forecast_mae"], {"legend": "{{station}} {{fuel}}"}),
        ("Forecast source", ["sum by (source) (rate(forecast_source_total[1m]))"], {"legend": "{{source}}"}),
        ("Decision cycle p95", ['histogram_quantile(0.95, sum by (le) (rate(decision_cycle_seconds_bucket[5m])))'], {"unit": "s", "legend": "cycle"}),
        ("Jev / Gemini latency p95", ['histogram_quantile(0.95, sum by (le) (rate(jev_latency_seconds_bucket[5m])))',
                                      'histogram_quantile(0.95, sum by (le) (rate(gemini_latency_seconds_bucket[5m])))'], {"unit": "s", "legend": "{{__name__}}"}),
        ("Alerts by kind", ["sum by (kind) (increase(alerts_total[5m]))"], {"legend": "{{kind}}"}),
        ("Operator overrides", ["operator_overrides_total"], {"kind": "stat", "legend": "overrides"}),
    ]),
    dash("fsp-infra", "Infrastructure", [
        ("CPU % per container", ['100 * sum by (name) (rate(container_cpu_usage_seconds_total{name=~".+"}[1m]))'], {"unit": "percent", "legend": "{{name}}"}),
        ("Memory per container", ['sum by (name) (container_memory_usage_bytes{name=~".+"})'], {"unit": "bytes", "legend": "{{name}}"}),
        ("Disk I/O per container", ['sum by (name) (rate(container_fs_writes_bytes_total{name=~".+"}[1m]) + rate(container_fs_reads_bytes_total{name=~".+"}[1m]))'], {"unit": "Bps", "legend": "{{name}}"}),
        ("Network rx per container", ['sum by (name) (rate(container_network_receive_bytes_total{name=~".+"}[1m]))'], {"unit": "Bps", "legend": "{{name}}"}),
    ]),
    dash("fsp-loadtest", "Load Test", [
        ("RPS by route", ['sum by (service, route) (rate(http_requests_total[30s]))'], {"unit": "reqps", "legend": "{{service}} {{route}}"}),
        ("p50/p95/p99 by service", [
            'histogram_quantile(0.50, sum by (le, service) (rate(http_request_duration_seconds_bucket[30s])))',
            P95.replace("[1m]", "[30s]"),
            'histogram_quantile(0.99, sum by (le, service) (rate(http_request_duration_seconds_bucket[30s])))'], {"unit": "s"}),
        ("Error rate %", ['100 * sum(rate(http_requests_total{status=~"5.."}[30s])) / clamp_min(sum(rate(http_requests_total[30s])), 1e-9)'], {"unit": "percent", "legend": "5xx %"}),
        ("CPU % per container", ['100 * sum by (name) (rate(container_cpu_usage_seconds_total{name=~".+"}[30s]))'], {"unit": "percent", "legend": "{{name}}"}),
    ]),
]

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for d in DASHBOARDS:
        (OUT / f"{d['uid']}.json").write_text(json.dumps(d, indent=1), encoding="utf-8")
    print(f"wrote {len(DASHBOARDS)} dashboards to {OUT}")
