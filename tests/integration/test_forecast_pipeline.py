"""Phase 4 acceptance: forecast-svc against the REAL stack (simulator -> ingestion-svc -> Postgres -> forecast-svc).

Needs ``docker compose up`` (simulator, postgres, redis, ingestion-svc, forecast-svc). For a meaningful adaptation
test run the simulator at demo speed:   SIMULATION_SPEED=2 docker compose up -d

    SIM_LIVE_URL=http://localhost:8000 INGESTION_URL=http://localhost:18101 FORECAST_URL=http://localhost:18102 \
    TEST_DATABASE_URL=postgresql+asyncpg://fuel_admin:change-me@127.0.0.1:55432/fuel_platform \
    pytest tests/integration/test_forecast_pipeline.py -s

WARNING: resets the simulator (admin endpoints) and steps it ~600 ticks; results are written to
``docs/data/phase4_live_results.json``.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fsp_shared.sim_client import SimClient

SIM = os.environ.get("SIM_LIVE_URL")
ING = os.environ.get("INGESTION_URL")
FC = os.environ.get("FORECAST_URL")
DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not (SIM and ING and FC and DB), reason="SIM_LIVE_URL / INGESTION_URL / FORECAST_URL / TEST_DATABASE_URL not set"),
]
RESULTS = Path(__file__).resolve().parents[2] / "docs" / "data" / "phase4_live_results.json"


async def wait_for(predicate, timeout, interval=0.25, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = predicate()
        if asyncio.iscoroutine(r):
            r = await r
        if r:
            return r
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {message}")


async def step_to(admin, target):
    while (await admin.instance()).data.tick < target:
        await admin.admin_step()


async def test_forecast_service_end_to_end():
    admin = SimClient(SIM, 10.0)
    engine = create_async_engine(DB)
    http = httpx.AsyncClient(timeout=10.0)
    results: dict = {"simulation_speed_note": "adaptation measured against the simulator's configured SIMULATION_SPEED"}

    async def demand_max_tick():
        async with engine.connect() as conn:
            return (await conn.execute(text("SELECT coalesce(max(tick), -1) FROM demand_observations"))).scalar_one()

    async def forecast(**body):
        r = await http.post(f"{FC}/forecast", json=body)
        assert r.status_code == 200, r.text
        return r.json()

    try:
        versions_before = {r["version"] for r in (await http.get(f"{FC}/models")).json()["registry"]}  # earlier runs
        await admin.admin_clear_faults()
        await admin.admin_pause()
        await admin.admin_reset()
        await admin.admin_pause()
        await wait_for(lambda: _synced(http, ING, 0), 20, message="ingestion synced the reset world (tick 0)")

        # ---- A: cold start -> BASELINE_FALLBACK (spec 10.6) -----------------------------------------------------
        await step_to(admin, 50)
        await wait_for(lambda: _synced(http, ING, 50), 20, message="ingestion at tick 50")
        cold = await wait_for(lambda: _fc_at(http, FC, 50), 15, message="forecast-svc cache at tick 50")
        assert {f["source"] for f in cold["forecasts"]} == {"BASELINE_FALLBACK"}
        assert all(f["model_version"] is None for f in cold["forecasts"])  # <96 rows per pair: no pair uses a model
        tongi = next(f for f in cold["forecasts"] if f["station_id"] == "station-tongi" and f["fuel_type"] == "DIESEL")
        assert tongi["horizon"][0]["tick"] == 51 and tongi["horizon"][0]["sigma"] == pytest.approx(0.08 * tongi["horizon"][0]["mean"])
        assert tongi["current_inventory"] is not None and tongi["burn_rate_lph"] > 0

        # ---- B: two-plus simulated days of history, train, validate against naive baselines --------------------
        await step_to(admin, 400)
        await wait_for(lambda: _synced(http, ING, 400), 40, message="ingestion at tick 400")
        await wait_for(lambda: _demand_at(engine, 399), 30, message="demand history backfilled")
        report = (await http.post(f"{FC}/train", timeout=60.0)).json()
        assert report["n_rows"] > 12 * 300 and not report["skipped"], report.get("skipped")
        results["training_report"] = report
        beats_last = sum(p["beats_naive_last"] for p in report["pairs"].values())
        beats_formula = sum(p["beats_naive_formula"] for p in report["pairs"].values())
        results["beats_naive_last"], results["beats_naive_formula"] = beats_last, beats_formula
        print(f"\nmean MAE {report['mean_mae']:.3f} L/tick | strongest naive baseline {report['mean_baseline_mae']:.3f} | "
              f"beats last-value {beats_last}/12, beats formula {beats_formula}/12")
        assert beats_last == 12  # a lag-based forecast is never competitive with the calendar model
        for key, p in report["pairs"].items():
            assert p["mae"] <= p["naive_formula_mae"] * 1.05, (key, p["mae"], p["naive_formula_mae"])  # never materially worse
        models = (await http.get(f"{FC}/models")).json()
        assert models["active"] is not None and len(models["active"]["pairs"]) == 12 and models["registry"]

        warm = await wait_for(lambda: _fc_at(http, FC, 400), 15, message="forecast at tick 400")
        assert {f["source"] for f in warm["forecasts"]} == {"MODEL"} and warm["model_version"] == models["active"]["version"]

        # ---- C: latency (spec: all 12 pairs, p95 < 30 ms after warmup) --------------------------------------------------
        # Server-side numbers come from the service's own Prometheus histogram (http_request_duration_seconds), taken
        # per phase. NOTE: on Docker Desktop for Windows a POST whose headers and body arrive in separate TCP writes
        # stalls ~40 ms in the port-forwarding proxy (delayed ACK) -- the server then legitimately waits for the body,
        # so POST-with-body from a Windows host looks slow in BOTH client and server timings while an empty POST, a GET
        # or a single-write POST take ~2 ms. The p95 assertion therefore uses GET; POST is reported for transparency.
        for _ in range(20):
            await http.get(f"{FC}/forecast")
        b0 = await _forecast_buckets(http, FC)
        get_ms, post_ms = [], []
        for _ in range(300):
            t0 = time.perf_counter()
            r = await http.get(f"{FC}/forecast")
            get_ms.append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 200 and len(r.json()["forecasts"]) == 12
        b1 = await _forecast_buckets(http, FC)
        for _ in range(100):
            t0 = time.perf_counter()
            r = await http.post(f"{FC}/forecast", json={})
            post_ms.append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 200
        b2 = await _forecast_buckets(http, FC)

        def frac(lo, hi, key):
            return (hi[key] - lo[key]) / (hi["+Inf"] - lo["+Inf"])

        results["latency"] = {
            "server_side_get": {"requests": b1["+Inf"] - b0["+Inf"], "fraction_le_10ms": frac(b0, b1, "0.01"),
                                "fraction_le_25ms": frac(b0, b1, "0.025")},
            "server_side_post": {"requests": b2["+Inf"] - b1["+Inf"], "fraction_le_25ms": frac(b1, b2, "0.025"),
                                 "note": "includes the proxy's ~40 ms header/body stall (Docker Desktop on Windows)"},
            "client_get_ms": {"p50": float(np.percentile(get_ms, 50)), "p95": float(np.percentile(get_ms, 95)),
                              "p99": float(np.percentile(get_ms, 99)), "n": len(get_ms)},
            "client_post_ms": {"p50": float(np.percentile(post_ms, 50)), "p95": float(np.percentile(post_ms, 95))},
        }
        lat = results["latency"]
        print(f"/forecast GET server-side: {lat['server_side_get']['fraction_le_10ms']:.1%} <= 10 ms, "
              f"{lat['server_side_get']['fraction_le_25ms']:.1%} <= 25 ms; client GET p50 {lat['client_get_ms']['p50']:.1f} ms "
              f"p95 {lat['client_get_ms']['p95']:.1f} ms; client POST p50 {lat['client_post_ms']['p50']:.1f} ms (proxy stall)")
        assert lat["server_side_get"]["fraction_le_25ms"] >= 0.95  # p95 <= 25 ms bucket bound => p95 < 30 ms
        assert lat["client_get_ms"]["p95"] < 30.0

        # ---- D: demand_spike 1.8x -> forecasts adapt within <= 4 ticks --------------------------------------------------
        await admin.admin_run()
        await wait_for(lambda: _tick_gt(admin, 402), 15, message="simulator running")
        before = await forecast(station_ids=["station-tongi"], fuel_types=["DIESEL"], horizon=12)
        base_by_hour = {p["tick"] % 96: p["mean"] for p in before["forecasts"][0]["horizon"]}
        inj_tick = (await admin.instance()).data.tick
        start = inj_tick + 1
        await admin.admin_create_event("demand_spike", start, 30, {"station_ids": ["station-tongi"], "multiplier": 1.8})
        probe_tick = start + 3  # a tick well inside the spike
        adapted_at = None
        t_wall = time.monotonic()
        while time.monotonic() - t_wall < 30:
            f = await forecast(station_ids=["station-tongi"], fuel_types=["DIESEL"], horizon=12)
            pts = {p["tick"]: p["mean"] for p in f["forecasts"][0]["horizon"]}
            if probe_tick in pts and probe_tick % 96 in base_by_hour:
                # compare with the un-spiked forecast for the same clock time (same hour factor, adjacent ticks differ <2%)
                if pts[probe_tick] > 1.5 * base_by_hour[probe_tick % 96]:
                    adapted_at = f["tick"]
                    wall = time.monotonic() - t_wall
                    break
            await asyncio.sleep(0.1)
        assert adapted_at is not None, "forecast never reflected the 1.8x spike"
        lag_ticks = adapted_at - inj_tick
        results["spike_adaptation"] = {"injected_at_tick": inj_tick, "forecast_adapted_at_tick": adapted_at,
                                       "ticks_to_adapt": lag_ticks, "wall_seconds": wall}
        print(f"spike adaptation: {lag_ticks} ticks ({wall:.2f} s wall)")
        assert lag_ticks <= 4, results["spike_adaptation"]
        tongi_after = f["forecasts"][0]
        assert tongi_after["source"] == "MODEL"
        await admin.admin_pause()

        # ---- E: scheduled retraining (every 96 ticks) --------------------------------------------------------------------
        await step_to(admin, 620)
        await wait_for(lambda: _synced(http, ING, 620), 40, message="ingestion at tick 620")
        await wait_for(lambda: _new_versions_gt(http, FC, versions_before, 3), 30, message="periodic retraining registered new versions")
        reg = (await http.get(f"{FC}/models")).json()["registry"]
        new = sorted((r for r in reg if r["version"] not in versions_before), key=lambda r: r["trained_at_tick"])
        trained = [r["trained_at_tick"] for r in new]
        results["registry_trained_at_ticks"] = trained
        assert len(trained) >= 3 and max(trained) >= 500, trained
        auto = [t for t in trained if t != report["trained_at_tick"]]  # every version except our explicit POST /train
        assert all(b - a >= 96 for a, b in zip(auto, auto[1:])), auto  # scheduler never retrains sooner than 96 ticks
        assert sum(1 for r in reg if r["active"]) == 1
    finally:
        RESULTS.parent.mkdir(parents=True, exist_ok=True)
        RESULTS.write_text(json.dumps(results, indent=2, default=float), encoding="utf-8")
        await admin.admin_pause()
        await admin.admin_reset()
        await admin.admin_pause()
        await admin.aclose()
        await http.aclose()
        await engine.dispose()


# --- small async predicates -------------------------------------------------------------------------------------------
async def _synced(http, ing, tick):
    return (await http.get(f"{ing}/status")).json().get("last_synced_tick") == tick


async def _fc_at(http, fc, tick):
    r = await http.post(f"{fc}/forecast", json={})
    return r.json() if r.status_code == 200 and r.json()["tick"] == tick else None


async def _demand_at(engine, tick):
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT coalesce(max(tick), -1) FROM demand_observations"))).scalar_one() >= tick


async def _forecast_buckets(http, fc):
    """Cumulative bucket counts of http_request_duration_seconds for GET/POST /forecast, summed over methods."""
    import re

    text_ = (await http.get(f"{fc}/metrics")).text
    out: dict[str, float] = {}
    for m in re.finditer(r'^http_request_duration_seconds_bucket\{le="([^"]+)",route="/forecast",service="forecast-svc"\} ([0-9.e+]+)$', text_, re.M):
        out[m.group(1)] = float(m.group(2))
    return {"0.01": out["0.01"], "0.025": out["0.025"], "+Inf": out["+Inf"]}


async def _tick_gt(admin, tick):
    return (await admin.instance()).data.tick > tick


async def _new_versions_gt(http, fc, before, n):
    reg = (await http.get(f"{fc}/models")).json()["registry"]
    return len([r for r in reg if r["version"] not in before]) >= n
