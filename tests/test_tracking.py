"""Local-only MLflow projection of authoritative training metadata (TOOLS-08)."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from ibkr_trader.signals import tracking


class FakeMlflow:
    def __init__(self):
        self.calls = {}

    def set_tracking_uri(self, uri):
        self.calls["tracking_uri"] = uri

    def set_experiment(self, name):
        self.calls["experiment"] = name
        return SimpleNamespace(experiment_id="experiment-1")

    @contextmanager
    def start_run(self, **kwargs):
        self.calls["start_run"] = kwargs
        yield SimpleNamespace(info=SimpleNamespace(run_id="run-123"))

    def log_params(self, params):
        self.calls["params"] = params

    def log_metrics(self, metrics):
        self.calls["metrics"] = metrics

    def log_artifact(self, path):
        self.calls["artifact"] = path


def _metadata():
    return {
        "model": "ml_lt",
        "version": "v8",
        "feature_set_version": 2,
        "seed": 19,
        "universe": {"sha256_16": "abc123", "n_symbols": 180},
        "train_window": {"n_rows": 12000, "n_dates": 72},
        "validation": {
            "overall_ic": {
                "lightgbm": {"mean": 0.04, "std": 0.1, "n_dates": 36},
                "ridge": {"mean": 0.07, "std": 0.12, "n_dates": 36},
            }
        },
        "lgbm_params": {"num_leaves": 7, "verbose": -1},
        "lgbm_search": {
            "method": "optuna",
            "duration_seconds": 12.5,
            "completed_count": 40,
            "pruned_count": 10,
        },
    }


def test_log_training_run_uses_only_local_store_and_exact_metadata_artifact(tmp_path, monkeypatch):
    artifact_dir = tmp_path / "models" / "ml_lt" / "v8"
    artifact_dir.mkdir(parents=True)
    metadata_path = artifact_dir / "metadata.json"
    metadata_path.write_text(json.dumps(_metadata(), indent=2))
    fake = FakeMlflow()
    monkeypatch.setattr(tracking, "_require_mlflow", lambda: fake)

    result = tracking.log_training_run(artifact_dir, tmp_path / "mlruns")

    assert result.run_id == "run-123"
    assert os.environ[tracking._ALLOW_FILE_STORE_ENV] == "true"
    assert result.tracking_uri == (tmp_path / "mlruns").resolve().as_uri()
    assert fake.calls["tracking_uri"].startswith("file:")
    assert fake.calls["experiment"] == tracking.EXPERIMENT_NAME
    assert fake.calls["start_run"] == {
        "experiment_id": "experiment-1",
        "run_name": "ml_lt-v8",
        "tags": {
            "metadata_authority": "versioned-artifact",
            "artifact_dir": str(artifact_dir.resolve()),
        },
    }
    assert fake.calls["artifact"] == str(metadata_path)
    assert json.loads(metadata_path.read_text()) == _metadata()
    assert fake.calls["params"]["artifact_version"] == "v8"
    assert fake.calls["params"]["lgbm.num_leaves"] == 7
    assert fake.calls["metrics"]["ic.ridge.mean"] == pytest.approx(0.07)
    assert fake.calls["metrics"]["search.completed_count"] == 40.0


def test_log_training_run_refuses_missing_or_non_object_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(tracking, "_require_mlflow", lambda: FakeMlflow())
    with pytest.raises(FileNotFoundError, match="missing"):
        tracking.log_training_run(tmp_path / "absent", tmp_path / "mlruns")

    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    (artifact_dir / "metadata.json").write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        tracking.log_training_run(artifact_dir, tmp_path / "mlruns")


def test_missing_mlflow_has_install_hint(monkeypatch):
    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr(tracking.importlib, "import_module", missing)
    with pytest.raises(RuntimeError, match="uv sync --extra ml --extra tracking"):
        tracking._require_mlflow()


def test_real_mlflow_file_store_round_trip(tmp_path):
    mlflow = pytest.importorskip("mlflow")
    artifact_dir = tmp_path / "models" / "ml_lt" / "v8"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "metadata.json").write_text(json.dumps(_metadata(), indent=2))

    result = tracking.log_training_run(artifact_dir, tmp_path / "mlruns")

    client = mlflow.MlflowClient(tracking_uri=result.tracking_uri)
    run = client.get_run(result.run_id)
    assert run.data.params["artifact_version"] == "v8"
    assert float(run.data.metrics["ic.ridge.mean"]) == pytest.approx(0.07)
    downloaded = client.download_artifacts(
        result.run_id, "metadata.json", dst_path=str(tmp_path / "downloaded")
    )
    assert json.loads(Path(downloaded).read_text()) == _metadata()
