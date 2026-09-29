"""Training, model registry and activation rule (spec §10.7).

A *version* is a bundle of one ridge model per (station, fuel). The registry row stores the bundle's mean validation
MAE/RMSE and the mean MAE of the stronger naive baseline; a new version is activated only if it beats the active one
on the same time-ordered holdout.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fsp_shared import world_constants as wc
from fsp_shared.demand import EventSpec
from fsp_shared.logging import get_logger
from fsp_shared.timeutil import parse_sim_time  # noqa: F401  (re-exported for tests)

from . import features as F
from . import metrics as m
from .model import MIN_TRAIN_ROWS, ModelBundle, PairModel, fit_pair, holdout_mae

log = get_logger("forecast.trainer")

RETRAIN_EVERY_TICKS = 96  # one simulated day at 15-minute ticks


class TrainingError(Exception):
    pass


@dataclass
class TrainingInputs:
    current_tick: int
    tick_minutes: int
    series: dict[tuple[str, str], pd.DataFrame]
    events: list[EventSpec]
    outage_ticks: dict[str, set[int]]


@dataclass
class TrainReport:
    version: str
    trained_at_tick: int
    n_rows: int
    mean_mae: float
    mean_rmse: float
    mean_baseline_mae: float
    activated: bool
    reason: str
    previous_version: str | None
    previous_mean_mae_on_holdout: float | None = None
    pairs: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.__dict__, default=float))


class ModelHolder:
    """The active bundle; ``on_change`` lets the service drop memoised responses."""

    def __init__(self) -> None:
        self.bundle: ModelBundle | None = None
        self.on_change: Any = None

    def set(self, bundle: ModelBundle | None) -> None:
        self.bundle = bundle
        if self.on_change:
            self.on_change()


# --------------------------------------------------------------------------------------------------
# data loading
# --------------------------------------------------------------------------------------------------
async def load_training_inputs(engine: AsyncEngine) -> TrainingInputs:
    async with engine.connect() as conn:
        tick_row = (await conn.execute(text("SELECT tick, tick_minutes FROM sim_ticks ORDER BY tick DESC LIMIT 1"))).first()
        if tick_row is None:
            raise TrainingError("no simulator state has been ingested yet")
        obs = (
            await conn.execute(
                text(
                    "SELECT station_id, fuel_type, tick, sim_time, demand_liters FROM demand_observations "
                    "WHERE demand_liters IS NOT NULL ORDER BY station_id, fuel_type, tick"
                )
            )
        ).all()
        ev = (await conn.execute(text("SELECT type, start_tick, end_tick, status, parameters FROM sim_events"))).all()
        out = (
            await conn.execute(text("SELECT DISTINCT station_id, tick FROM station_snapshots WHERE status = 'OUTAGE'"))
        ).all()

    frame = pd.DataFrame(obs, columns=["station_id", "fuel_type", "tick", "sim_time", "demand_liters"])
    series = {
        (str(s), str(f)): g[["tick", "sim_time", "demand_liters"]].reset_index(drop=True)
        for (s, f), g in frame.groupby(["station_id", "fuel_type"])
    }
    outage: dict[str, set[int]] = {}
    for station_id, tick in out:
        outage.setdefault(station_id, set()).add(int(tick))
    events = [
        EventSpec(e.type, int(e.start_tick), int(e.end_tick), e.parameters or {}, e.status) for e in ev
    ]
    return TrainingInputs(int(tick_row.tick), int(tick_row.tick_minutes), series, events, outage)


def build_frames(inputs: TrainingInputs) -> tuple[dict[tuple[str, str], pd.DataFrame], dict[str, str]]:
    """Usable (outage-free) training frame per pair, plus the pairs skipped and why."""
    frames: dict[tuple[str, str], pd.DataFrame] = {}
    skipped: dict[str, str] = {}
    for station_id in wc.STATIONS:
        for fuel in wc.FUEL_TYPES:
            key = (station_id, fuel)
            obs = inputs.series.get(key)
            if obs is None or len(obs) < MIN_TRAIN_ROWS:
                skipped[f"{station_id}/{fuel}"] = f"{0 if obs is None else len(obs)} observations"
                continue
            frame = F.build_training_frame(
                station_id,
                fuel,
                obs,
                inputs.tick_minutes,
                inputs.events,
                inputs.outage_ticks.get(station_id, ()),
            )
            frame = frame[~frame["outage"]].reset_index(drop=True)  # exclude OUTAGE rows (spec §10.2)
            if len(frame) < MIN_TRAIN_ROWS:
                skipped[f"{station_id}/{fuel}"] = f"{len(frame)} usable rows"
                continue
            frames[key] = frame
    return frames, skipped


# --------------------------------------------------------------------------------------------------
# training (CPU-bound; run in a thread)
# --------------------------------------------------------------------------------------------------
def train_candidate(inputs: TrainingInputs, version: str) -> tuple[ModelBundle, dict[tuple[str, str], pd.DataFrame], dict[str, str]]:
    frames, skipped = build_frames(inputs)
    if not frames:
        raise TrainingError(f"not enough data to train any pair (need >= {MIN_TRAIN_ROWS} rows): {skipped}")
    pairs: dict[tuple[str, str], PairModel] = {
        key: fit_pair(key[0], key[1], frame) for key, frame in frames.items()
    }
    bundle = ModelBundle(
        version=version,
        trained_at_tick=inputs.current_tick,
        tick_minutes=inputs.tick_minutes,
        pairs=pairs,
        n_rows=int(sum(len(f) for f in frames.values())),
    )
    return bundle, frames, skipped


def compare_with_active(
    candidate: ModelBundle, active: ModelBundle | None, frames: dict[tuple[str, str], pd.DataFrame]
) -> tuple[bool, str, float | None]:
    """Activate only if the candidate beats the active bundle on the candidate's validation slice."""
    if active is None:
        return True, "no active model", None
    if active.tick_minutes != candidate.tick_minutes:
        return True, "tick_minutes changed", None
    cand_maes, act_maes = [], []
    for key, pm in candidate.pairs.items():
        cand_maes.append(pm.metrics["mae"])
        prev = active.pairs.get(key)
        act_maes.append(
            holdout_mae(prev, frames[key], pm.metrics["val_start_tick"]) if prev is not None else float("inf")
        )
    cand_mean = sum(cand_maes) / len(cand_maes)
    act_mean = sum(act_maes) / len(act_maes)
    if cand_mean < act_mean:
        return True, f"candidate MAE {cand_mean:.3f} < active {act_mean:.3f}", act_mean
    return False, f"candidate MAE {cand_mean:.3f} >= active {act_mean:.3f}", act_mean


class Trainer:
    def __init__(self, engine: AsyncEngine, model_dir: Path, holder: ModelHolder) -> None:
        self.engine = engine
        self.model_dir = model_dir
        self.holder = holder
        self._lock = asyncio.Lock()
        self.last_report: TrainReport | None = None
        self.last_train_tick: int | None = None  # tick of the last *attempt* (activated or not)

    # ------------------------------------------------------------------ registry
    async def load_active(self) -> ModelBundle | None:
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT version, artifact_path FROM model_registry WHERE active ORDER BY created_at DESC LIMIT 1")
                )
            ).first()
        if row is None or not row.artifact_path:
            return None
        path = Path(row.artifact_path)
        if not path.exists():
            log.warning("active_model_artifact_missing", version=row.version, path=str(path))
            return None
        bundle = ModelBundle.load(path)
        self.holder.set(bundle)
        self.last_train_tick = bundle.trained_at_tick  # a restart must not trigger an immediate retrain
        self._publish_metrics(bundle)
        log.info("active_model_loaded", version=bundle.version, trained_at_tick=bundle.trained_at_tick)
        return bundle

    async def list_models(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT version, trained_at_tick, n_rows, mae, rmse, baseline_mae, artifact_path, active, created_at "
                        "FROM model_registry ORDER BY created_at DESC"
                    )
                )
            ).mappings().all()
        return [dict(r) for r in rows]

    async def _register(self, bundle: ModelBundle, path: Path, activate: bool) -> None:
        async with self.engine.begin() as conn:
            if activate:
                await conn.execute(text("UPDATE model_registry SET active = false WHERE active"))
            await conn.execute(
                text(
                    "INSERT INTO model_registry (version, trained_at_tick, n_rows, mae, rmse, baseline_mae, artifact_path, active) "
                    "VALUES (:v, :t, :n, :mae, :rmse, :bmae, :path, :active)"
                ),
                {
                    "v": bundle.version,
                    "t": bundle.trained_at_tick,
                    "n": bundle.n_rows,
                    "mae": bundle.mean_mae,
                    "rmse": bundle.mean_rmse,
                    "bmae": bundle.mean_baseline_mae,
                    "path": str(path),
                    "active": activate,
                },
            )

    @staticmethod
    def _publish_metrics(bundle: ModelBundle) -> None:
        for (station, fuel), pm in bundle.pairs.items():
            m.FORECAST_MAE.labels(station, fuel).set(pm.metrics["mae"])
            m.FORECAST_BASELINE_MAE.labels(station, fuel).set(
                min(pm.metrics["naive_last_mae"], pm.metrics["naive_formula_mae"])
            )
        m.ACTIVE_MODEL_TICK.set(bundle.trained_at_tick)

    # ------------------------------------------------------------------ scheduling
    def should_retrain(self, current_tick: int, min_rows: int) -> bool:
        """Retrain every RETRAIN_EVERY_TICKS ticks *since the last attempt* — whether or not that attempt beat the
        active model (otherwise a losing candidate would be retrained every scheduler pass)."""
        if min_rows < MIN_TRAIN_ROWS:
            return False
        last = self.last_train_tick
        if last is None and self.holder.bundle is not None:
            last = self.holder.bundle.trained_at_tick
        if last is None:
            return True
        if current_tick < last:  # simulator was reset: history restarted, train once on the new timeline
            return True
        return current_tick - last >= RETRAIN_EVERY_TICKS

    # ------------------------------------------------------------------ training
    async def train(self) -> TrainReport:
        async with self._lock:
            started = time.perf_counter()
            try:
                inputs = await load_training_inputs(self.engine)
                version = f"ridge-t{inputs.current_tick}-{int(time.time() * 1000)}"
                candidate, frames, skipped = await asyncio.to_thread(train_candidate, inputs, version)
                active = self.holder.bundle
                activate, reason, prev_mae = await asyncio.to_thread(compare_with_active, candidate, active, frames)
                path = self.model_dir / f"{version}.joblib"
                await asyncio.to_thread(candidate.save, path)
                await self._register(candidate, path, activate)
                if activate:
                    self.holder.set(candidate)
                    self._publish_metrics(candidate)
                self.last_train_tick = inputs.current_tick
                m.TRAININGS.labels("activated" if activate else "kept_active").inc()
            except Exception:
                m.TRAININGS.labels("failed").inc()
                raise
            finally:
                m.TRAINING_SECONDS.observe(time.perf_counter() - started)
            report = TrainReport(
                version=candidate.version,
                trained_at_tick=candidate.trained_at_tick,
                n_rows=candidate.n_rows,
                mean_mae=candidate.mean_mae,
                mean_rmse=candidate.mean_rmse,
                mean_baseline_mae=candidate.mean_baseline_mae,
                activated=activate,
                reason=reason,
                previous_version=active.version if active else None,
                previous_mean_mae_on_holdout=prev_mae,
                pairs={
                    f"{s}/{f}": {k: v for k, v in pm.metrics.items() if k != "alpha_grid_mae"}
                    | {"residual_sigma": pm.residual_sigma, "sigma_rel": pm.sigma_rel}
                    for (s, f), pm in candidate.pairs.items()
                },
                skipped=skipped,
                duration_s=round(time.perf_counter() - started, 3),
            )
            self.last_report = report
            log.info(
                "training_done",
                version=report.version,
                activated=activate,
                reason=reason,
                mean_mae=round(report.mean_mae, 3),
                baseline_mae=round(report.mean_baseline_mae, 3),
                duration_s=report.duration_s,
            )
            return report
