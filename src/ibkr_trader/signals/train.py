"""Training harness for the long-term model (ML-03): LightGBM + a linear sanity floor.

Runs the walk-forward evaluation (signals.validation) over the supervised dataset
(signals.dataset), fits a final LightGBM model on the full window, and writes a versioned
artifact directory::

    models/ml_lt/<vN>/model.txt        # LightGBM booster
    models/ml_lt/<vN>/ridge.joblib     # numeric ridge pipeline
    models/ml_lt/<vN>/metadata.json    # features, label spec, fold ICs, versions, …
    models/ml_lt/latest                # text file naming the newest version

lightgbm/scikit-learn live behind the ``[ml]`` extra — this module imports without them and
fails with an install hint only when training is actually attempted. Reads Postgres only.
"""

import hashlib
import itertools
import json
import platform
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from ibkr_trader.signals.dataset import (
    LABEL_HORIZON_MONTHS,
    build_dataset,
    feature_columns,
)
from ibkr_trader.signals.eligibility import EligibilityLimits
from ibkr_trader.signals.features import FEATURE_SET_VERSION
from ibkr_trader.signals.validation import (
    Fold,
    FoldResult,
    SupervisedModel,
    evaluate_walk_forward,
    summarize_ics,
    walk_forward_folds,
)

MODEL_NAME = "ml_lt"
MODEL_FILE = "model.txt"
RIDGE_FILE = "ridge.joblib"
METADATA_FILE = "metadata.json"
LATEST_MARKER = "latest"

#: Printed with every run/report so nobody mistakes a lucky fold for a strategy.
HONEST_EXPECTATIONS = (
    "Mean OOS rank IC of 0.03-0.05 is good in this domain; ~0 is the common honest outcome. "
    "The decision metric remains the after-cost backtest (ML-04), not IC."
)

#: Fixed non-capacity parameters. The three capacity parameters are walk-forward selected.
DEFAULT_LGBM_PARAMS: dict[str, Any] = {
    "n_estimators": 100,
    "learning_rate": 0.05,
    "num_leaves": 7,
    "min_child_samples": 50,
    "subsample": 0.9,
    "subsample_freq": 1,
    "colsample_bytree": 0.9,
    "verbose": -1,
}

LGBM_CAPACITY_GRID: dict[str, tuple[int, ...]] = {
    "num_leaves": (7, 15, 31),
    "min_child_samples": (20, 50, 100),
    "n_estimators": (100, 200, 400),
}

#: Inclusive integer ranges matching the old grid's lower/upper bounds. Optuna may sample
#: every integer inside them; widening the parameter set itself remains an explicit follow-up.
LGBM_OPTUNA_SEARCH_SPACE: dict[str, range] = {
    "num_leaves": range(7, 32),
    "min_child_samples": range(20, 101),
    "n_estimators": range(100, 401),
}

_CATEGORICAL_FEATURES = ("sector",)  # encoded as LightGBM native categoricals


def _require_ml() -> None:
    try:
        import lightgbm  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "training needs the ML extra — install with: pip install -e .[ml]"
        ) from exc


def _require_optuna() -> None:
    try:
        import optuna  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "Optuna search needs the ML extra — install with: pip install -e .[ml]"
        ) from exc


def _require_sklearn() -> None:
    try:
        import joblib  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "ridge training/prediction needs the ML extra — install with: pip install -e .[ml]"
        ) from exc


def _sklearn_major_minor(version: str) -> tuple[int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)", version)
    return (int(match.group(1)), int(match.group(2))) if match else None


def assert_sklearn_compatible(trained_version: str, runtime_version: str | None = None) -> None:
    """Reject a ridge pickle produced by another scikit-learn major/minor release."""
    _require_sklearn()
    runtime_version = runtime_version or importlib_metadata.version("scikit-learn")
    trained = _sklearn_major_minor(trained_version)
    runtime = _sklearn_major_minor(runtime_version)
    if trained is None or runtime is None or trained != runtime:
        raise RuntimeError(
            "ridge artifact scikit-learn version mismatch "
            f"(trained with {trained_version}, running {runtime_version}); "
            "retrain with `ibkr-trader train run` before scoring"
        )


def encode_categoricals(x: pd.DataFrame, categories: dict[str, list[str]]) -> pd.DataFrame:
    """Cast categorical feature columns to pandas ``category`` with **fixed** category lists.

    LightGBM stores categoricals by code, so predict-time frames must use the exact category
    list the model was trained with (recorded in metadata) — unseen values become NaN, which
    LightGBM treats as missing.
    """
    x = x.copy()
    for column, cats in categories.items():
        if column in x.columns:
            x[column] = pd.Categorical(x[column], categories=cats)
    return x


class LgbmModel:
    """LightGBM regressor on the full feature set (NaN-native, categorical ``sector``)."""

    def __init__(self, params: dict[str, Any] | None = None, *, seed: int = 42):
        _require_ml()
        self.params: dict[str, Any] = dict(DEFAULT_LGBM_PARAMS) | dict(params or {})
        self.seed = seed
        self.categories: dict[str, list[str]] = {}
        self._model: Any = None

    def fit(self, x: pd.DataFrame, y: pd.Series) -> None:
        from lightgbm import LGBMRegressor

        self.categories = {
            column: sorted(x[column].dropna().unique())
            for column in _CATEGORICAL_FEATURES
            if column in x.columns
        }
        self._model = LGBMRegressor(random_state=self.seed, **self.params)
        self._model.fit(encode_categoricals(x, self.categories), y)

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        assert self._model is not None, "fit before predict"
        return np.asarray(self._model.predict(encode_categoricals(x, self.categories)))

    def save(self, path: Path) -> None:
        assert self._model is not None, "fit before save"
        self._model.booster_.save_model(str(path))


class RidgeModel:
    """Trivial linear sanity floor: numeric features only, median-impute + standardize."""

    def __init__(self, *, alpha: float = 1.0, seed: int = 42):
        _require_sklearn()
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        self._columns: list[str] = []
        self._pipeline = make_pipeline(
            SimpleImputer(strategy="median", keep_empty_features=True),
            StandardScaler(),
            Ridge(alpha=alpha, random_state=seed),
        )

    def _numeric(self, x: pd.DataFrame) -> pd.DataFrame:
        return x[self._columns].astype(float)

    def fit(self, x: pd.DataFrame, y: pd.Series) -> None:
        self._columns = [c for c in x.columns if c not in _CATEGORICAL_FEATURES]
        self._pipeline.fit(self._numeric(x), y)

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return np.asarray(self._pipeline.predict(self._numeric(x)))

    @property
    def columns(self) -> list[str]:
        return list(self._columns)

    def save(self, path: Path) -> None:
        import joblib

        if not self._columns:
            raise RuntimeError("fit before save")
        joblib.dump({"columns": self._columns, "pipeline": self._pipeline}, path)

    @classmethod
    def load(cls, path: Path, *, trained_sklearn_version: str) -> "RidgeModel":
        assert_sklearn_compatible(trained_sklearn_version)
        import joblib

        payload = joblib.load(path)
        model = cls.__new__(cls)
        model._columns = list(payload["columns"])
        model._pipeline = payload["pipeline"]
        return model


@dataclass
class TrainResult:
    version: str
    artifact_dir: Path
    fold_results: list[FoldResult]
    metadata: dict


def _next_version(model_dir: Path) -> str:
    existing = [
        int(match.group(1))
        for child in model_dir.glob("v*")
        if child.is_dir() and (match := re.fullmatch(r"v(\d+)", child.name))
    ]
    return f"v{max(existing, default=0) + 1}"


def _library_versions() -> dict[str, str]:
    versions = {"python": platform.python_version()}
    for package in ("lightgbm", "scikit-learn", "numpy", "pandas"):
        try:
            versions[package] = importlib_metadata.version(package)
        except importlib_metadata.PackageNotFoundError:  # pragma: no cover
            versions[package] = "not installed"
    return versions


def _universe_hash(universe: Sequence[str]) -> str:
    canonical = "\n".join(sorted({symbol.upper() for symbol in universe}))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _fold_payload(results: Sequence[FoldResult], model_names: Sequence[str]) -> list[dict]:
    return [
        {
            "fold_id": r.fold_id,
            "train": [r.train_start.isoformat(), r.train_end.isoformat()],
            "test": [r.test_start.isoformat(), r.test_end.isoformat()],
            "n_train": r.n_train,
            "n_test": r.n_test,
            "ic": {
                name: {
                    "mean": r.ic_mean(name),
                    "std": r.ic_std(name),
                    "n_dates": len(r.ics_by_model.get(name, [])),
                }
                for name in model_names
            },
        }
        for r in results
    ]


def select_lgbm_params(
    df: pd.DataFrame,
    folds: Sequence,
    *,
    seed: int = 42,
    grid: Mapping[str, Sequence[int]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select LightGBM capacity using mean fold IC only (lower std breaks ties)."""
    capacity_grid: Mapping[str, Sequence[int]] = grid or LGBM_CAPACITY_GRID
    keys = ("num_leaves", "min_child_samples", "n_estimators")
    candidates: list[dict[str, Any]] = []
    for index, values in enumerate(itertools.product(*(capacity_grid[key] for key in keys))):
        capacity = dict(zip(keys, values, strict=True))
        name = f"candidate_{index:02d}"
        results = evaluate_walk_forward(
            df,
            folds,
            {name: partial(LgbmModel, capacity, seed=seed)},
        )
        fold_ics = [result.ic_mean(name) for result in results]
        valid = [value for value in fold_ics if value is not None]
        candidates.append(
            {
                "params": capacity,
                "mean_fold_ic": float(np.mean(valid)) if valid else None,
                "std_fold_ic": float(np.std(valid)) if valid else None,
                "n_folds": len(valid),
            }
        )
    if not any(candidate["mean_fold_ic"] is not None for candidate in candidates):
        raise ValueError("LightGBM grid produced no valid fold ICs")

    def selection_key(candidate: dict[str, Any]) -> tuple[float, float]:
        mean = candidate["mean_fold_ic"]
        std = candidate["std_fold_ic"]
        return (
            float(mean) if mean is not None else -np.inf,
            -(float(std) if std is not None else np.inf),
        )

    winner = max(candidates, key=selection_key)
    selected = dict(DEFAULT_LGBM_PARAMS) | dict(winner["params"])
    return selected, {
        "selection_metric": "mean fold rank IC",
        "tie_break": "lower std of fold rank IC",
        "grid": {key: list(capacity_grid[key]) for key in keys},
        "candidates": candidates,
        "winner": winner,
    }


def _normalise_optuna_space(
    search_space: Mapping[str, Sequence[int]] | None,
) -> tuple[dict[str, tuple[int, ...]], dict[str, dict[str, Any]]]:
    keys = ("num_leaves", "min_child_samples", "n_estimators")
    raw = LGBM_OPTUNA_SEARCH_SPACE if search_space is None else search_space
    if set(raw) != set(keys):
        raise ValueError(f"Optuna search space must contain exactly: {', '.join(keys)}")
    values_by_key: dict[str, tuple[int, ...]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for key in keys:
        values = tuple(raw[key])
        if not values or any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise ValueError(f"Optuna search space {key!r} must contain integers")
        if len(values) != len(set(values)):
            raise ValueError(f"Optuna search space {key!r} contains duplicate values")
        ordered = tuple(sorted(values))
        values_by_key[key] = ordered
        contiguous = ordered == tuple(range(ordered[0], ordered[-1] + 1))
        metadata[key] = (
            {"type": "int", "low": ordered[0], "high": ordered[-1], "step": 1}
            if contiguous
            else {"type": "categorical", "choices": list(ordered)}
        )
    return values_by_key, metadata


def _evaluate_lgbm_fold(
    df: pd.DataFrame,
    fold: Fold,
    params: dict[str, Any],
    *,
    seed: int,
) -> float | None:
    """Evaluate one candidate/fold, kept separate so pruning is unit-testable."""
    name = "candidate"
    result = evaluate_walk_forward(
        df,
        [fold],
        {name: partial(LgbmModel, params, seed=seed)},
    )[0]
    return result.ic_mean(name)


def select_lgbm_params_optuna(
    df: pd.DataFrame,
    folds: Sequence[Fold],
    *,
    seed: int,
    n_trials: int = 50,
    search_space: Mapping[str, Sequence[int]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select LightGBM capacity with deterministic TPE and median fold pruning.

    ``search_space`` is primarily an equivalence-test seam. Consecutive values become an
    inclusive integer range; non-consecutive values become exact categorical choices.
    """
    _require_ml()
    _require_optuna()
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if not folds:
        raise ValueError("Optuna search needs at least one walk-forward fold")

    import optuna
    from optuna.trial import TrialState

    values_by_key, space_metadata = _normalise_optuna_space(search_space)
    sampler = optuna.samplers.TPESampler(seed=seed)
    pruner = optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=1)
    study = optuna.create_study(direction="maximize", sampler=sampler, pruner=pruner)
    started = time.perf_counter()

    # A caller can provide a small finite space for an oracle/equivalence run. Cover it once
    # when the trial budget permits; the production default remains sampled integer ranges.
    if search_space is not None:
        exact_candidates = list(itertools.product(*(values_by_key[key] for key in values_by_key)))
        if len(exact_candidates) <= n_trials:
            for values in exact_candidates:
                study.enqueue_trial(dict(zip(values_by_key, values, strict=True)))

    def objective(trial) -> float:
        capacity: dict[str, Any] = {}
        for key, values in values_by_key.items():
            spec = space_metadata[key]
            if spec["type"] == "int":
                capacity[key] = trial.suggest_int(key, values[0], values[-1])
            else:
                capacity[key] = trial.suggest_categorical(key, list(values))

        fold_ics: list[float | None] = []
        for step, fold in enumerate(folds):
            fold_ics.append(_evaluate_lgbm_fold(df, fold, capacity, seed=seed))
            valid = [value for value in fold_ics if value is not None]
            running_mean = float(np.mean(valid)) if valid else -np.inf
            trial.set_user_attr("fold_ics", fold_ics)
            trial.set_user_attr("mean_fold_ic", float(np.mean(valid)) if valid else None)
            trial.set_user_attr("std_fold_ic", float(np.std(valid)) if valid else None)
            trial.set_user_attr("n_folds", len(valid))
            trial.report(running_mean, step=step)
            if trial.should_prune():
                raise optuna.TrialPruned()

        valid = [value for value in fold_ics if value is not None]
        return float(np.mean(valid)) if valid else -np.inf

    study.optimize(objective, n_trials=n_trials, n_jobs=1, show_progress_bar=False)
    duration_seconds = time.perf_counter() - started

    candidates = [
        {
            "number": trial.number,
            "state": trial.state.name.lower(),
            "params": {key: int(value) for key, value in trial.params.items()},
            "mean_fold_ic": trial.user_attrs.get("mean_fold_ic"),
            "std_fold_ic": trial.user_attrs.get("std_fold_ic"),
            "n_folds": trial.user_attrs.get("n_folds", 0),
            "fold_ics": trial.user_attrs.get("fold_ics", []),
        }
        for trial in study.trials
    ]
    completed = [
        candidate
        for candidate, trial in zip(candidates, study.trials, strict=True)
        if trial.state == TrialState.COMPLETE and candidate["mean_fold_ic"] is not None
    ]
    if not completed:
        raise ValueError("LightGBM Optuna search produced no valid fold ICs")

    def selection_key(candidate: dict[str, Any]) -> tuple[float, float]:
        return (
            float(candidate["mean_fold_ic"]),
            -float(candidate["std_fold_ic"]),
        )

    winner = max(completed, key=selection_key)
    selected = dict(DEFAULT_LGBM_PARAMS) | dict(winner["params"])
    return selected, {
        "method": "optuna",
        "selection_metric": "mean fold rank IC",
        "tie_break": "lower std of fold rank IC",
        "sampler": "TPESampler",
        "pruner": "MedianPruner",
        "seed": seed,
        "n_trials": n_trials,
        "completed_count": sum(trial.state == TrialState.COMPLETE for trial in study.trials),
        "pruned_count": sum(trial.state == TrialState.PRUNED for trial in study.trials),
        "duration_seconds": duration_seconds,
        "search_space": space_metadata,
        "trials": candidates,
        "best_trial": winner,
        "winner": winner,
    }


def train_on_dataset(
    df: pd.DataFrame,
    *,
    models_dir: Path,
    universe: Sequence[str],
    window: tuple[datetime, datetime],
    seed: int = 42,
    test_size: int = 6,
    min_train: int = 24,
    lgbm_params: dict[str, Any] | None = None,
    benchmark_symbol: str = "XEQT",
    min_history_days: int = 252,
    search: str = "optuna",
    n_trials: int = 50,
) -> TrainResult:
    """Walk-forward evaluate, fit the final model on the full window, write the artifact.

    Pure over the dataset frame (no DB) so tests drive it with synthetic panels. The final
    LightGBM model trains on **all** labeled rows; fold ICs are its honest out-of-sample
    record and are stored alongside it in metadata.
    """
    _require_ml()
    if df.empty:
        raise ValueError("dataset is empty — ingest more history or widen the window")
    features = feature_columns(df)
    folds = walk_forward_folds(
        sorted(df["date"].unique()), test_size=test_size, min_train=min_train
    )
    if search not in {"optuna", "grid"}:
        raise ValueError("search must be 'optuna' or 'grid'")
    if lgbm_params is None and search == "optuna":
        selected_lgbm_params, search_metadata = select_lgbm_params_optuna(
            df, folds, seed=seed, n_trials=n_trials
        )
    elif lgbm_params is None:
        started = time.perf_counter()
        selected_lgbm_params, search_metadata = select_lgbm_params(df, folds, seed=seed)
        search_metadata = {
            "method": "grid",
            "duration_seconds": time.perf_counter() - started,
            **search_metadata,
        }
    else:
        selected_lgbm_params = dict(DEFAULT_LGBM_PARAMS) | dict(lgbm_params)
        search_metadata = {
            "method": "explicit",
            "selection_metric": "explicit parameters (grid skipped)",
            "tie_break": None,
            "grid": None,
            "candidates": [],
            "winner": {"params": dict(lgbm_params)},
        }
    factories: dict[str, Callable[[], SupervisedModel]] = {
        "lightgbm": lambda: LgbmModel(selected_lgbm_params, seed=seed),
        "ridge": lambda: RidgeModel(seed=seed),
    }
    fold_results = evaluate_walk_forward(df, folds, factories)

    final = LgbmModel(selected_lgbm_params, seed=seed)
    final.fit(df[features], df["label"])
    final_ridge = RidgeModel(seed=seed)
    final_ridge.fit(df[features], df["label"])

    model_dir = models_dir / MODEL_NAME
    model_dir.mkdir(parents=True, exist_ok=True)
    version = _next_version(model_dir)
    artifact_dir = model_dir / version
    artifact_dir.mkdir()
    final.save(artifact_dir / MODEL_FILE)
    final_ridge.save(artifact_dir / RIDGE_FILE)

    model_names = list(factories)
    library_versions = _library_versions()
    metadata = {
        "model": MODEL_NAME,
        "version": version,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "model_file": MODEL_FILE,
        "ridge": {
            "model_file": RIDGE_FILE,
            "numeric_feature_columns": final_ridge.columns,
            "sklearn_version": library_versions["scikit-learn"],
        },
        "feature_set_version": FEATURE_SET_VERSION,
        "feature_columns": features,
        "categorical": {
            column: {"encoding": "lightgbm-native-categorical", "categories": cats}
            for column, cats in final.categories.items()
        },
        "label": {
            "spec": (
                f"{LABEL_HORIZON_MONTHS}-month forward total return in CAD (ADJUSTED_LAST "
                f"bars) in excess of {benchmark_symbol}, percentile-ranked cross-sectionally "
                "per monthly rebalance date into [0, 1]"
            ),
            "horizon_months": LABEL_HORIZON_MONTHS,
            "benchmark": benchmark_symbol,
        },
        "universe": {"n_symbols": len(set(universe)), "sha256_16": _universe_hash(universe)},
        "eligibility": {"min_history_days": min_history_days},
        "train_window": {
            "requested": [window[0].isoformat(), window[1].isoformat()],
            "dataset_dates": [
                min(df["date"]).isoformat(),
                max(df["date"]).isoformat(),
            ],
            "n_rows": len(df),
            "n_dates": int(df["date"].nunique()),
        },
        "validation": {
            "scheme": "expanding walk-forward",
            "test_size_months": test_size,
            "min_train_months": min_train,
            "purge_months": LABEL_HORIZON_MONTHS,
            "folds": _fold_payload(fold_results, model_names),
            "overall_ic": {name: summarize_ics(fold_results, name) for name in model_names},
        },
        "lgbm_params": {**final.params, "random_state": seed},
        "lgbm_search": search_metadata,
        # Kept for older artifact readers; new code should use the generic key above.
        "lgbm_grid_search": search_metadata if search_metadata["method"] != "optuna" else None,
        "seed": seed,
        "library_versions": library_versions,
        "note": HONEST_EXPECTATIONS,
    }
    (artifact_dir / METADATA_FILE).write_text(json.dumps(metadata, indent=2))
    (model_dir / LATEST_MARKER).write_text(version)
    return TrainResult(
        version=version, artifact_dir=artifact_dir, fold_results=fold_results, metadata=metadata
    )


def train_from_db(
    session: Session,
    universe: Sequence[str],
    start: datetime,
    end: datetime,
    *,
    models_dir: Path = Path("models"),
    benchmark_symbol: str = "XEQT",
    seed: int = 42,
    test_size: int = 6,
    min_train: int = 24,
    lgbm_params: dict[str, Any] | None = None,
    limits: EligibilityLimits | None = None,
    search: str = "optuna",
    n_trials: int = 50,
) -> TrainResult:
    """Build the dataset from Postgres and run ``train_on_dataset`` (the CLI entry point)."""
    _require_ml()
    df = build_dataset(
        session,
        universe,
        start,
        end,
        benchmark_symbol=benchmark_symbol,
        limits=limits,
    )
    return train_on_dataset(
        df,
        models_dir=models_dir,
        universe=universe,
        window=(start, end),
        seed=seed,
        test_size=test_size,
        min_train=min_train,
        lgbm_params=lgbm_params,
        benchmark_symbol=benchmark_symbol,
        min_history_days=(limits or EligibilityLimits()).min_history_days,
        search=search,
        n_trials=n_trials,
    )


def load_latest_metadata(models_dir: Path = Path("models")) -> dict:
    """Metadata of the newest artifact, via the ``latest`` marker (for ``train report``)."""
    model_dir = models_dir / MODEL_NAME
    marker = model_dir / LATEST_MARKER
    if not marker.exists():
        raise FileNotFoundError(
            f"no trained model found under {model_dir} — run `ibkr-trader train run` first"
        )
    version = marker.read_text().strip()
    metadata_path = model_dir / version / METADATA_FILE
    if not metadata_path.exists():
        raise FileNotFoundError(f"latest marker points at {version!r} but {metadata_path} is gone")
    return json.loads(metadata_path.read_text())
