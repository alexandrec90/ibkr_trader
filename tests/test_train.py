"""End-to-end training smoke (ML-03): synthetic panel → dataset → walk-forward ICs for
LightGBM + the ridge floor → versioned artifact on disk → report reads it back. Skips
cleanly when the [ml] extra isn't installed."""

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

lightgbm = pytest.importorskip("lightgbm")
pytest.importorskip("sklearn")

from ibkr_trader.backtest.engine import Series  # noqa: E402
from ibkr_trader.signals.dataset import build_dataset_from_panel, feature_columns  # noqa: E402
from ibkr_trader.signals.eligibility import EligibilityLimits  # noqa: E402
from ibkr_trader.signals.features import FEATURE_SET_VERSION, CorporateData  # noqa: E402
from ibkr_trader.signals.train import (  # noqa: E402
    DEFAULT_LGBM_PARAMS,
    MODEL_FILE,
    encode_categoricals,
    load_latest_metadata,
    train_on_dataset,
)

OPEN_LIMITS = EligibilityLimits(min_price=0.0, min_avg_dollar_volume=0.0, min_history_days=1)
FAST_LGBM = {"n_estimators": 40, "min_child_samples": 5}
WINDOW = (datetime(2018, 1, 1, tzinfo=UTC), datetime(2023, 1, 1, tzinfo=UTC))


def _series(iid: int, symbol: str, n: int, drift: float, wobble: float) -> Series:
    dates = [date(2018, 1, 1) + timedelta(days=i) for i in range(n)]
    closes = [100.0 * math.exp(drift * i) * (1.0 + 0.04 * math.sin(i / wobble)) for i in range(n)]
    return Series(
        instrument_id=iid,
        symbol=symbol,
        currency="CAD",
        exchange="TSX",
        dates=dates,
        opens=list(closes),
        closes=list(closes),
        volumes=[1e6] * n,
    )


def _dataset():
    n = 1830  # ~60 months of daily bars → ~47 labeled monthly dates
    universe = {
        iid: _series(iid, f"S{iid}", n, drift=0.00005 * iid, wobble=5.0 + iid)
        for iid in range(1, 9)
    }
    benchmark = _series(99, "XEQT", n, drift=0.0002, wobble=9.0)
    sectors = ("Technology", "Financials", "Energy", None)
    corporate = {iid: CorporateData(sector=sectors[iid % len(sectors)]) for iid in universe}
    df = build_dataset_from_panel(
        universe,
        benchmark,
        corporate=corporate,
        start=date(2018, 1, 1),
        end=date(2023, 12, 31),
        limits=OPEN_LIMITS,
    )
    assert len(df) > 300
    return df


def test_train_writes_a_versioned_artifact_and_report_reads_it_back(tmp_path: Path):
    df = _dataset()
    result = train_on_dataset(
        df,
        models_dir=tmp_path,
        universe=[f"S{i}" for i in range(1, 9)],
        window=WINDOW,
        seed=7,
        test_size=6,
        min_train=12,
        lgbm_params=FAST_LGBM,
    )

    # artifact on disk: model + metadata + latest marker
    assert result.version == "v1"
    assert result.artifact_dir == tmp_path / "ml_lt" / "v1"
    assert (result.artifact_dir / MODEL_FILE).exists()
    assert (tmp_path / "ml_lt" / "latest").read_text() == "v1"
    metadata = json.loads((result.artifact_dir / "metadata.json").read_text())
    assert metadata == result.metadata

    # metadata records what a saved model must know about itself
    assert metadata["feature_set_version"] == FEATURE_SET_VERSION
    assert metadata["feature_columns"] == feature_columns(df)
    assert metadata["label"]["horizon_months"] == 12
    assert "percentile-ranked" in metadata["label"]["spec"]
    assert metadata["universe"]["n_symbols"] == 8
    assert metadata["categorical"]["sector"]["encoding"] == "lightgbm-native-categorical"
    assert set(metadata["categorical"]["sector"]["categories"]) == {
        "Technology",
        "Financials",
        "Energy",
    }
    assert metadata["lgbm_params"]["n_estimators"] == 40
    assert metadata["lgbm_params"]["random_state"] == 7
    assert {"python", "lightgbm", "scikit-learn"} <= set(metadata["library_versions"])

    # walk-forward record: per-fold ICs for LightGBM AND the linear sanity floor
    folds = metadata["validation"]["folds"]
    assert len(folds) >= 2
    for fold in folds:
        assert set(fold["ic"]) == {"lightgbm", "ridge"}
        for model in ("lightgbm", "ridge"):
            assert fold["ic"][model]["n_dates"] > 0
            assert -1.0 <= fold["ic"][model]["mean"] <= 1.0
    assert metadata["validation"]["purge_months"] == 12
    assert metadata["validation"]["overall_ic"]["ridge"]["n_dates"] > 0

    # the saved booster is loadable and predicts finite scores for encoded features
    booster = lightgbm.Booster(model_file=str(result.artifact_dir / MODEL_FILE))
    categories = {column: spec["categories"] for column, spec in metadata["categorical"].items()}
    x = encode_categoricals(df[metadata["feature_columns"]], categories)
    predictions = booster.predict(x)
    assert len(predictions) == len(df)
    assert all(math.isfinite(p) for p in predictions)

    # report path reads the same metadata back through the latest marker
    assert load_latest_metadata(tmp_path) == metadata


def test_second_run_bumps_the_version_and_moves_latest(tmp_path: Path):
    df = _dataset()
    kwargs = dict(
        models_dir=tmp_path,
        universe=["S1"],
        window=WINDOW,
        test_size=6,
        min_train=12,
        lgbm_params=FAST_LGBM,
    )
    first = train_on_dataset(df, **kwargs)
    second = train_on_dataset(df, **kwargs)
    assert (first.version, second.version) == ("v1", "v2")
    assert (tmp_path / "ml_lt" / "latest").read_text() == "v2"
    assert load_latest_metadata(tmp_path)["version"] == "v2"
    assert (tmp_path / "ml_lt" / "v1" / MODEL_FILE).exists()  # old artifacts are kept


def test_load_latest_without_any_artifact_is_a_clear_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="train run"):
        load_latest_metadata(tmp_path)


def test_default_params_stay_light():
    # hyperparameter search is out of scope (plan); defaults + light tuning only
    assert DEFAULT_LGBM_PARAMS["verbose"] == -1
    assert "early_stopping_rounds" not in DEFAULT_LGBM_PARAMS
