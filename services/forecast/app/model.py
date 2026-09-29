"""Ridge models (spec §10.3-10.4): training uses ``Pipeline([StandardScaler(), Ridge(alpha)])``; serving compiles the
fitted pipeline into plain numpy weights so a 24-step recursive forecast for all 12 (station, fuel) pairs costs
milliseconds (a scikit-learn ``predict`` call per step would not fit the 30 ms budget).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from fsp_shared.demand import EventLike, noise_of, spike_active, spike_multiplier
from fsp_shared.schemas import HorizonPoint
from fsp_shared.timeutil import project_sim_time
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from . import features as F

ALPHA_GRID = (0.01, 0.1, 1.0, 10.0, 100.0, 1e3, 1e4, 1e5)  # large alphas shrink to the pure calendar baseline
VAL_FRACTION = 0.2
MIN_TRAIN_ROWS = 96  # spec §10.6: fewer rows -> baseline


class LinearPredictor:
    """``y = ((x - mean) / scale) . coef + intercept + x[baseline]`` — a fitted scaler + ridge *residual* on the
    calendar baseline (D19), without sklearn overhead. Output is multiplier-normalised demand (D18)."""

    __slots__ = ("baseline_idx", "coef", "intercept", "mean", "scale")

    def __init__(
        self, mean: np.ndarray, scale: np.ndarray, coef: np.ndarray, intercept: float, baseline_idx: int = F.BASELINE_IDX
    ) -> None:
        self.mean, self.scale, self.coef, self.intercept = mean, scale, coef, float(intercept)
        self.baseline_idx = baseline_idx

    @classmethod
    def from_pipeline(cls, pipe: Pipeline) -> LinearPredictor:
        scaler: StandardScaler = pipe.named_steps["scale"]
        ridge: Ridge = pipe.named_steps["ridge"]
        scale = np.where(scaler.scale_ == 0, 1.0, scaler.scale_)
        return cls(scaler.mean_.copy(), scale.copy(), np.ravel(ridge.coef_).copy(), float(np.ravel(ridge.intercept_)[0]))

    def predict(self, x: np.ndarray) -> float:
        return float(((x - self.mean) / self.scale) @ self.coef + self.intercept + x[self.baseline_idx])

    def predict_many(self, x: np.ndarray) -> np.ndarray:
        return ((x - self.mean) / self.scale) @ self.coef + self.intercept + x[:, self.baseline_idx]


def make_pipeline(alpha: float) -> Pipeline:
    return Pipeline([("scale", StandardScaler()), ("ridge", Ridge(alpha=alpha))])


@dataclass
class PairModel:
    station_id: str
    fuel_type: str
    alpha: float
    pipeline: Pipeline  # residual model, refit on all usable rows (used for serving)
    residual_sigma: float  # validation RMSE, liters
    sigma_rel: float  # RMSE / mean validation prediction (heteroscedastic scaling, see DECISIONS D17)
    metrics: dict[str, Any] = field(default_factory=dict)
    predictor: LinearPredictor = field(init=False)

    def __post_init__(self) -> None:
        self.predictor = LinearPredictor.from_pipeline(self.pipeline)

    def __getstate__(self) -> dict[str, Any]:  # the compiled predictor is rebuilt on load
        state = dict(self.__dict__)
        state.pop("predictor", None)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self.predictor = LinearPredictor.from_pipeline(self.pipeline)

    def forecast(
        self,
        lag: F.LagState,
        current_tick: int,
        current_sim_time: Any,
        tick_minutes: int,
        horizon: int,
        events: Sequence[EventLike],
    ) -> list[HorizonPoint]:
        """Recursive multi-step forecast: predict t+1, feed it back as ``d(t)``, recompute lags, continue."""
        points: list[HorizonPoint] = []
        # work on a copy so the caller's state is not mutated
        state = F.LagState(lag.e4, lag.e16, list(lag.last5))
        for k in range(1, horizon + 1):
            tick = current_tick + k
            hour = project_sim_time(current_sim_time, current_tick, tick, tick_minutes).hour
            mult = spike_multiplier(self.station_id, tick, events)
            x = F.feature_row(
                self.station_id,
                self.fuel_type,
                hour,
                tick_minutes,
                mult,
                spike_active(self.station_id, tick, events),
                state.e4,
                state.e16,
                state.d_prev1(),
                state.d_prev5(),
            )
            norm = max(0.0, self.predictor.predict(x))  # demand cannot be negative
            mean = norm * mult  # the known multiplier is applied structurally (D18)
            points.append(HorizonPoint(tick=tick, mean=mean, sigma=self.sigma_rel * mean))
            state.push(norm)  # lags live in multiplier-normalised space
        return points


@dataclass
class ModelBundle:
    """One model per (station, fuel) plus the metadata registered in ``model_registry``."""

    version: str
    trained_at_tick: int
    tick_minutes: int
    pairs: dict[tuple[str, str], PairModel]
    n_rows: int = 0

    def get(self, station_id: str, fuel_type: str) -> PairModel | None:
        return self.pairs.get((station_id, fuel_type))

    @property
    def mean_mae(self) -> float:
        vals = [m.metrics["mae"] for m in self.pairs.values()]
        return float(np.mean(vals)) if vals else float("nan")

    @property
    def mean_rmse(self) -> float:
        vals = [m.metrics["rmse"] for m in self.pairs.values()]
        return float(np.mean(vals)) if vals else float("nan")

    @property
    def mean_baseline_mae(self) -> float:
        """Mean over pairs of the *stronger* naive baseline (lower of last-value and formula)."""
        vals = [min(m.metrics["naive_last_mae"], m.metrics["naive_formula_mae"]) for m in self.pairs.values()]
        return float(np.mean(vals)) if vals else float("nan")

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        joblib.dump(self, tmp)
        tmp.replace(path)

    @staticmethod
    def load(path: Path) -> ModelBundle:
        bundle = joblib.load(path)
        if not isinstance(bundle, ModelBundle):
            raise TypeError(f"{path} does not contain a ModelBundle")
        return bundle


# --------------------------------------------------------------------------------------------------
# fitting
# --------------------------------------------------------------------------------------------------
def _mae(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean(np.abs(y - p)))


def _rmse(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.sqrt(np.mean((y - p) ** 2)))


def fit_pair(station_id: str, fuel_type: str, frame: pd.DataFrame, alphas: Iterable[float] = ALPHA_GRID) -> PairModel:
    """Fit one (station, fuel) model on a training frame (outage rows already removed).

    Alpha is tuned on a time-ordered validation split (last 20 %, never shuffled); the returned model is then refit
    on every row with the chosen alpha, while ``metrics`` describe the honest validation performance.
    """
    n = len(frame)
    if n < MIN_TRAIN_ROWS:
        raise ValueError(f"{station_id}/{fuel_type}: {n} usable rows < {MIN_TRAIN_ROWS}")
    cut = int(round(n * (1 - VAL_FRACTION)))
    train, val = frame.iloc[:cut], frame.iloc[cut:]
    x_tr = train[F.FEATURES].to_numpy()
    y_tr = train["y_norm"].to_numpy() - train["baseline"].to_numpy()  # residual on the calendar baseline (D19)
    x_va, y_va = val[F.FEATURES].to_numpy(), val["y"].to_numpy()  # scored in raw liters
    m_va = val["demand_multiplier"].to_numpy()

    best: tuple[float, float, np.ndarray] | None = None  # (mae, alpha, predictions)
    grid: dict[str, float] = {}
    for alpha in alphas:
        pipe = make_pipeline(alpha).fit(x_tr, y_tr)
        pred = np.maximum(pipe.predict(x_va) + x_va[:, F.BASELINE_IDX], 0.0) * m_va
        mae = _mae(y_va, pred)
        grid[str(alpha)] = mae
        if best is None or mae < best[0]:
            best = (mae, alpha, pred)
    assert best is not None
    mae, alpha, pred = best
    rmse = _rmse(y_va, pred)
    naive_last = _mae(y_va, val["naive_last"].to_numpy())
    naive_formula = _mae(y_va, val["naive_formula"].to_numpy())

    final = make_pipeline(alpha).fit(frame[F.FEATURES].to_numpy(), (frame["y_norm"] - frame["baseline"]).to_numpy())
    coefs = dict(zip(F.FEATURES, np.ravel(final.named_steps["ridge"].coef_).round(3).tolist(), strict=True))
    metrics = {
        "mae": mae,
        "rmse": rmse,
        "naive_last_mae": naive_last,
        "naive_formula_mae": naive_formula,
        "beats_naive_last": mae < naive_last,
        "beats_naive_formula": mae < naive_formula,
        "alpha": alpha,
        "alpha_grid_mae": grid,
        "n_train": len(train),
        "n_val": len(val),
        "val_start_tick": int(val["tick"].iloc[0]),
        "coefficients_scaled": coefs,
        "documented_noise": noise_of(station_id),
    }
    mean_pred = max(float(np.mean(pred)), 1e-9)
    return PairModel(
        station_id=station_id,
        fuel_type=fuel_type,
        alpha=alpha,
        pipeline=final,
        residual_sigma=rmse,
        sigma_rel=rmse / mean_pred,
        metrics=metrics,
    )


def holdout_mae(model: PairModel, frame: pd.DataFrame, val_start_tick: int) -> float:
    """MAE of an (already trained) model on the rows from ``val_start_tick`` on — used to compare against a candidate."""
    val = frame[frame["tick"] >= val_start_tick]
    if val.empty:
        return float("inf")
    norm = np.maximum(model.predictor.predict_many(val[F.FEATURES].to_numpy()), 0.0)
    return _mae(val["y"].to_numpy(), norm * val["demand_multiplier"].to_numpy())
