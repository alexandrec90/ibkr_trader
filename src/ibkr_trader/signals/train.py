"""Training harness for the long-term model (ML-03): LightGBM + a linear sanity floor.

Runs the walk-forward evaluation (signals.validation) over the supervised dataset
(signals.dataset), fits a final LightGBM model on the full window, and writes a versioned
artifact directory::

    models/ml_lt/<vN>/model.txt        # LightGBM booster
    models/ml_lt/<vN>/metadata.json    # features, label spec, fold ICs, versions, …
    models/ml_lt/latest                # text file naming the newest version

lightgbm/scikit-learn live behind the ``[ml]`` extra — this module imports without them and
fails with an install hint only when training is actually attempted. Reads Postgres only.
"""

import hashlib
import json
import platform
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
from ibkr_trader.signals.features import FEATURE_SET_VERSION
from ibkr_trader.signals.validation import (
    FoldResult,
    SupervisedModel,
    evaluate_walk_forward,
    summarize_ics,
    walk_forward_folds,
)

MODEL_NAME = "ml_lt"
MODEL_FILE = "model.txt"
METADATA_FILE = "metadata.json"
LATEST_MARKER = "latest"

#: Printed with every run/report so nobody mistakes a lucky fold for a strategy.
HONEST_EXPECTATIONS = (
    "Mean OOS rank IC of 0.03-0.05 is good in this domain; ~0 is the common honest outcome. "
    "The decision metric remains the after-cost backtest (ML-04), not IC."
)

#: LightGBM defaults + light tuning only (per plan) — hyperparameter search is out of scope.
DEFAULT_LGBM_PARAMS: dict[str, Any] = {
    "n_estimators": 400,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 20,
    "subsample": 0.9,
    "subsample_freq": 1,
    "colsample_bytree": 0.9,
    "verbose": -1,
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
        _require_ml()
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
    factories: dict[str, Callable[[], SupervisedModel]] = {
        "lightgbm": lambda: LgbmModel(lgbm_params, seed=seed),
        "ridge": lambda: RidgeModel(seed=seed),
    }
    fold_results = evaluate_walk_forward(df, folds, factories)

    final = LgbmModel(lgbm_params, seed=seed)
    final.fit(df[features], df["label"])

    model_dir = models_dir / MODEL_NAME
    model_dir.mkdir(parents=True, exist_ok=True)
    version = _next_version(model_dir)
    artifact_dir = model_dir / version
    artifact_dir.mkdir()
    final.save(artifact_dir / MODEL_FILE)

    model_names = list(factories)
    metadata = {
        "model": MODEL_NAME,
        "version": version,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "model_file": MODEL_FILE,
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
        "seed": seed,
        "library_versions": _library_versions(),
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
) -> TrainResult:
    """Build the dataset from Postgres and run ``train_on_dataset`` (the CLI entry point)."""
    _require_ml()
    df = build_dataset(session, universe, start, end, benchmark_symbol=benchmark_symbol)
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
