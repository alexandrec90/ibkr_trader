"""Optional local MLflow projection of authoritative training artifact metadata.

The versioned ``metadata.json`` written by :mod:`ibkr_trader.signals.train` remains the
source of truth.  This module reads that file after training, logs the exact file as an
MLflow artifact, and exposes a small set of derived parameters and metrics for comparison.
It never logs to a tracking server: callers provide a local directory and it is converted
to an explicit ``file:`` tracking URI.
"""

import importlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ibkr_trader.signals.train import METADATA_FILE

EXPERIMENT_NAME = "ibkr_trader.ml_lt"
_ALLOW_FILE_STORE_ENV = "MLFLOW_ALLOW_FILE_STORE"


@dataclass(frozen=True)
class TrackingRun:
    """Identity of one locally logged MLflow run."""

    run_id: str
    tracking_uri: str


def _require_mlflow():
    try:
        return importlib.import_module("mlflow")
    except ImportError as exc:  # pragma: no cover - depends on the optional environment
        raise RuntimeError(
            "MLflow tracking needs the tracking extra — install with: "
            "uv sync --extra ml --extra tracking"
        ) from exc


def _comparison_params(metadata: dict[str, Any]) -> dict[str, Any]:
    """Stable scalar fields useful as MLflow comparison-table columns."""
    window = metadata.get("train_window") or {}
    universe = metadata.get("universe") or {}
    search = metadata.get("lgbm_search") or {}
    params: dict[str, Any] = {
        "artifact_model": metadata.get("model", "unknown"),
        "artifact_version": metadata.get("version", "unknown"),
        "feature_set_version": metadata.get("feature_set_version", "unknown"),
        "seed": metadata.get("seed", "unknown"),
        "universe_hash": universe.get("sha256_16", "unknown"),
        "n_symbols": universe.get("n_symbols", "unknown"),
        "n_rows": window.get("n_rows", "unknown"),
        "n_dates": window.get("n_dates", "unknown"),
        "search_method": search.get("method", "unknown"),
    }
    for name, value in (metadata.get("lgbm_params") or {}).items():
        if isinstance(value, str | int | float | bool):
            params[f"lgbm.{name}"] = value
    return params


def _comparison_metrics(metadata: dict[str, Any]) -> dict[str, float]:
    """Numeric summaries derived from metadata; never an independent calculation."""
    metrics: dict[str, float] = {}
    overall = (metadata.get("validation") or {}).get("overall_ic") or {}
    for model_name, summary in overall.items():
        if not isinstance(summary, dict):
            continue
        for field in ("mean", "std", "n_dates"):
            value = summary.get(field)
            if isinstance(value, int | float) and not isinstance(value, bool):
                metrics[f"ic.{model_name}.{field}"] = float(value)
    search = metadata.get("lgbm_search") or {}
    for field in ("duration_seconds", "completed_count", "pruned_count"):
        value = search.get(field)
        if isinstance(value, int | float) and not isinstance(value, bool):
            metrics[f"search.{field}"] = float(value)
    return metrics


def log_training_run(
    artifact_dir: Path,
    tracking_dir: Path,
    *,
    experiment_name: str = EXPERIMENT_NAME,
) -> TrackingRun:
    """Project one completed artifact into a local MLflow file store.

    ``metadata.json`` is read from ``artifact_dir`` rather than accepted from the caller so
    the MLflow view cannot diverge from the persisted artifact. Model files are deliberately
    not duplicated into MLflow.
    """
    metadata_path = artifact_dir / METADATA_FILE
    if not metadata_path.is_file():
        raise FileNotFoundError(f"training artifact is missing {metadata_path}")
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read authoritative metadata {metadata_path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"authoritative metadata {metadata_path} must contain a JSON object")

    # MLflow 3.14 requires an explicit opt-in for its maintenance-mode FileStore. This is
    # intentional here: TOOLS-07 chose a local file backend and ruled out a tracking server.
    os.environ[_ALLOW_FILE_STORE_ENV] = "true"
    mlflow = _require_mlflow()
    local_store = tracking_dir.resolve()
    local_store.mkdir(parents=True, exist_ok=True)
    tracking_uri = local_store.as_uri()
    mlflow.set_tracking_uri(tracking_uri)
    experiment = mlflow.set_experiment(experiment_name)
    run_name = f"{metadata.get('model', 'model')}-{metadata.get('version', 'unknown')}"
    tags = {
        "metadata_authority": "versioned-artifact",
        "artifact_dir": str(artifact_dir.resolve()),
    }
    with mlflow.start_run(
        experiment_id=experiment.experiment_id,
        run_name=run_name,
        tags=tags,
    ) as run:
        mlflow.log_params(_comparison_params(metadata))
        metrics = _comparison_metrics(metadata)
        if metrics:
            mlflow.log_metrics(metrics)
        mlflow.log_artifact(str(metadata_path))
        run_id = str(run.info.run_id)
    return TrackingRun(run_id=run_id, tracking_uri=tracking_uri)
