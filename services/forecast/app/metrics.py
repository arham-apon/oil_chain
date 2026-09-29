"""Prometheus metrics owned by forecast-svc (names follow spec §10.8 / §15.1)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

FORECAST_MAE = Gauge("forecast_mae", "Validation MAE of the active model (liters/tick)", ["station", "fuel"])
FORECAST_BASELINE_MAE = Gauge(
    "forecast_baseline_mae", "Validation MAE of the stronger naive baseline", ["station", "fuel"]
)
FORECAST_INFERENCE = Histogram(
    "forecast_inference_seconds",
    "Time to compute a /forecast response (cache miss)",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.02, 0.03, 0.05, 0.1, 0.25, 0.5),
)
FORECAST_SOURCE = Counter("forecast_source_total", "Pair forecasts served by source", ["source"])
FORECAST_CACHE_HITS = Counter("forecast_cache_hits_total", "Responses served from the per-tick memo")
TRAINING_SECONDS = Histogram(
    "forecast_training_seconds", "Duration of a training run", buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60)
)
TRAININGS = Counter("forecast_trainings_total", "Training runs", ["outcome"])  # activated | kept_active | failed
ACTIVE_MODEL_TICK = Gauge("forecast_active_model_trained_at_tick", "Tick the active model was trained at")
STATE_AGE = Gauge("forecast_state_age_seconds", "Age of the in-memory world snapshot")
