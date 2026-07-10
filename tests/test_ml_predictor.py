"""ML-04 wiring: the trained ``ml_lt`` model as a first-class strategy.

Covers registry resolution (``get_allocator("ml_lt")``), artifact loading from a tiny stub
fixture (no LightGBM training in-test — the booster is injected), the feature-set-version
mismatch guard (zero scores + loud log), the clear error without the ``[ml]`` extra, and the
``feature_set_version`` pin in run params. Tests that construct the predictor need lightgbm
importable (its presence is checked at construction); the missing-extra test does not.
"""

import builtins
import importlib.util
import json
import logging
import math
from datetime import date, timedelta
from importlib import metadata as importlib_metadata
from pathlib import Path
from types import SimpleNamespace

import pytest

from ibkr_trader.backtest.engine import Series, simulate
from ibkr_trader.signals.eligibility import Candidate
from ibkr_trader.signals.features import FEATURE_SET_VERSION
from ibkr_trader.signals.portfolio import MlLtAllocator, MlLtRidgeAllocator, get_allocator
from ibkr_trader.signals.portfolio import available as allocators_available
from ibkr_trader.signals.predictor import MlLongTerm, MlLtRidge
from ibkr_trader.signals.predictor import available as predictors_available

requires_ml = pytest.mark.skipif(
    importlib.util.find_spec("lightgbm") is None, reason="needs the [ml] extra"
)

FEATURE_COLUMNS = ["momentum_12_1", "return_12m", "sector", "volatility"]
SECTORS = ["Energy", "Technology"]


def _make_artifact(
    tmp_path: Path,
    *,
    version: str = "v7",
    feature_set_version: str = FEATURE_SET_VERSION,
    sklearn_version: str | None = None,
) -> Path:
    """A stub models/ml_lt-style artifact dir: real metadata, fake model.txt (never parsed)."""
    root = tmp_path / "ml_lt"
    artifact = root / version
    artifact.mkdir(parents=True)
    (artifact / "model.txt").write_text("stub booster — tests inject a fake instead\n")
    (artifact / "ridge.joblib").write_text("stub ridge — tests inject a fake instead\n")
    sklearn_version = sklearn_version or importlib_metadata.version("scikit-learn")
    metadata = {
        "model": "ml_lt",
        "version": version,
        "model_file": "model.txt",
        "feature_set_version": feature_set_version,
        "feature_columns": FEATURE_COLUMNS,
        "ridge": {
            "model_file": "ridge.joblib",
            "numeric_feature_columns": ["momentum_12_1", "return_12m", "volatility"],
            "sklearn_version": sklearn_version,
        },
        "categorical": {
            "sector": {"encoding": "lightgbm-native-categorical", "categories": SECTORS}
        },
    }
    (artifact / "metadata.json").write_text(json.dumps(metadata))
    (root / "latest").write_text(version)
    return root


class FakeBooster:
    """Stands in for lightgbm.Booster: records frames, echoes ``return_12m`` as the rank."""

    def __init__(self, value: float | None = None):
        self.value = value
        self.frames: list = []

    def predict(self, x):
        self.frames.append(x)
        if self.value is not None:
            return [self.value]
        return list(x["return_12m"])


class FakeRidge:
    def __init__(self, value: float):
        self.value = value
        self.frames: list = []

    def predict(self, x):
        self.frames.append(x)
        return [self.value]


def test_ml_lt_is_registered_without_construction():
    # registration is an import side-effect and must not need lightgbm or an artifact
    assert "ml_lt" in predictors_available()
    assert "ml_lt" in allocators_available()
    assert "ml_lt_ridge" in predictors_available()
    assert "ml_lt_ridge" in allocators_available()


@requires_ml
def test_get_allocator_resolves_ml_lt_from_settings(tmp_path, monkeypatch):
    root = _make_artifact(tmp_path)
    monkeypatch.setattr(
        "ibkr_trader.config.get_settings", lambda: SimpleNamespace(ml_lt_model_dir=str(root))
    )
    allocator = get_allocator("ml_lt")
    assert isinstance(allocator, MlLtAllocator)
    assert allocator.name == "ml_lt"  # backtest_runs.strategy, not "score:ml_lt"
    assert allocator.version == "v7"  # the artifact version → pinned as model_version
    assert allocator.max_names == 15
    assert allocator.max_weight == 0.20


@requires_ml
def test_get_allocator_resolves_ml_lt_ridge_from_settings(tmp_path, monkeypatch):
    root = _make_artifact(tmp_path)
    monkeypatch.setattr(
        "ibkr_trader.config.get_settings", lambda: SimpleNamespace(ml_lt_model_dir=str(root))
    )
    allocator = get_allocator("ml_lt_ridge")
    assert isinstance(allocator, MlLtRidgeAllocator)
    assert allocator.name == "ml_lt_ridge"
    assert allocator.version == "v7"
    assert allocator.max_names == 15
    assert allocator.max_weight == 0.20


@requires_ml
def test_ridge_predict_centers_rank_and_uses_numeric_training_columns(tmp_path):
    predictor = MlLtRidge(model_dir=_make_artifact(tmp_path))
    fake = FakeRidge(0.8)
    predictor._model = fake
    assert predictor.predict({"return_12m": 0.2, "sector": "Energy"}) == pytest.approx(0.3)
    (frame,) = fake.frames
    assert list(frame.columns) == ["momentum_12_1", "return_12m", "volatility"]
    assert math.isnan(frame["momentum_12_1"].iloc[0])


@requires_ml
def test_predict_centers_predicted_rank_and_fills_missing_features(tmp_path):
    predictor = MlLongTerm(model_dir=_make_artifact(tmp_path))
    assert predictor.version == "v7"
    fake = FakeBooster(value=0.8)
    predictor._booster = fake

    score = predictor.predict({"return_12m": 0.2, "volatility": 0.3, "unknown_extra": 9.9})
    assert score == pytest.approx(0.8 - 0.5)

    (frame,) = fake.frames
    assert list(frame.columns) == FEATURE_COLUMNS  # training column order, exactly
    assert math.isnan(frame["momentum_12_1"].iloc[0])  # missing feature → NaN, not an error
    assert str(frame["sector"].dtype) == "category"
    assert list(frame["sector"].cat.categories) == SECTORS


@requires_ml
def test_ml_lt_allocator_holds_only_positive_scores(tmp_path, monkeypatch):
    root = _make_artifact(tmp_path)
    monkeypatch.setattr(
        "ibkr_trader.config.get_settings", lambda: SimpleNamespace(ml_lt_model_dir=str(root))
    )
    allocator = get_allocator("ml_lt")
    allocator._predictor._booster = FakeBooster()  # rank := return_12m

    def candidate(iid: int, symbol: str) -> Candidate:
        return Candidate(
            instrument_id=iid,
            symbol=symbol,
            currency="CAD",
            exchange="TSX",
            last_price=50.0,
            avg_dollar_volume=5e6,
            history_days=400,
        )

    candidates = [candidate(1, "AAA"), candidate(2, "BBB")]
    features = {1: {"return_12m": 0.9}, 2: {"return_12m": 0.2}}  # ranks 0.9 / 0.2 → +0.4 / -0.3
    weights = allocator.allocate(candidates, features)
    assert set(weights) == {1}
    assert 0 < weights[1] <= allocator.max_weight


@requires_ml
def test_feature_set_version_mismatch_scores_zero_and_logs(tmp_path, caplog):
    root = _make_artifact(tmp_path, feature_set_version="0")
    with caplog.at_level(logging.ERROR, logger="ibkr_trader.signals.predictor"):
        predictor = MlLongTerm(model_dir=root)
    assert "feature set" in caplog.text and "refusing to score" in caplog.text
    # zero score for every input, and the (stub) booster is never loaded/parsed
    assert predictor.predict({"return_12m": 0.9, "volatility": 0.1}) == 0.0
    assert predictor._booster is None


@requires_ml
def test_ridge_reuses_feature_set_mismatch_guard(tmp_path, caplog):
    root = _make_artifact(tmp_path, feature_set_version="0")
    with caplog.at_level(logging.ERROR, logger="ibkr_trader.signals.predictor"):
        predictor = MlLtRidge(model_dir=root)
    assert "ml_lt_ridge" in caplog.text and "refusing to score" in caplog.text
    assert predictor.predict({"return_12m": 0.9}) == 0.0
    assert predictor._model is None


@requires_ml
def test_ridge_rejects_sklearn_major_minor_mismatch(tmp_path):
    root = _make_artifact(tmp_path, sklearn_version="999.0.0")
    with pytest.raises(RuntimeError, match="version mismatch.*retrain"):
        MlLtRidge(model_dir=root)


@requires_ml
def test_missing_artifact_raises_with_train_hint(tmp_path):
    with pytest.raises(FileNotFoundError, match="train run"):
        MlLongTerm(model_dir=tmp_path / "nowhere")


def test_construct_without_ml_extra_raises_clear_error(tmp_path, monkeypatch):
    root = _make_artifact(tmp_path)
    real_import = builtins.__import__

    def no_lightgbm(name, *args, **kwargs):
        if name == "lightgbm" or name.startswith("lightgbm."):
            raise ImportError("No module named 'lightgbm'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_lightgbm)
    with pytest.raises(RuntimeError, match=r"pip install -e \.\[ml\]"):
        MlLongTerm(model_dir=root)


def test_construct_ridge_without_ml_extra_raises_clear_error(tmp_path, monkeypatch):
    root = _make_artifact(tmp_path)
    real_import = builtins.__import__

    def no_sklearn(name, *args, **kwargs):
        if name == "sklearn" or name.startswith("sklearn."):
            raise ImportError("No module named 'sklearn'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sklearn)
    with pytest.raises(RuntimeError, match=r"ml_lt_ridge.*pip install -e \.\[ml\]"):
        MlLtRidge(model_dir=root)


def test_simulate_pins_feature_set_version_into_params():
    calendar = [date(2020, 1, 6) + timedelta(days=i) for i in range(10)]
    series = Series(
        instrument_id=1,
        symbol="AAA",
        currency="CAD",
        exchange="TSX",
        dates=calendar,
        opens=[100.0] * len(calendar),
        closes=[100.0] * len(calendar),
        volumes=[1e6] * len(calendar),
    )
    allocator = get_allocator("equal_weight")
    result = simulate({1: series}, calendar, allocator)
    assert result.params["feature_set_version"] == FEATURE_SET_VERSION
    assert result.params["model_version"] == allocator.version
