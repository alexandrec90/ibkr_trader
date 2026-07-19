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
    LGBM_CAPACITY_GRID,
    MODEL_FILE,
    RIDGE_FILE,
    RidgeModel,
    encode_categoricals,
    load_latest_metadata,
    select_lgbm_params,
    train_on_dataset,
)
from ibkr_trader.signals.validation import walk_forward_folds  # noqa: E402

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


def test_train_writes_a_versioned_artifact_and_report_reads_it_back(tmp_path: Path, monkeypatch):
    df = _dataset()
    search_record = {
        "method": "optuna",
        "sampler": "TPESampler",
        "seed": 7,
        "n_trials": 3,
        "completed_count": 2,
        "pruned_count": 1,
        "duration_seconds": 1.25,
        "search_space": {"num_leaves": {"type": "int", "low": 7, "high": 31}},
        "best_trial": {"params": FAST_LGBM, "mean_fold_ic": 0.04},
        "winner": {"params": FAST_LGBM, "mean_fold_ic": 0.04},
    }
    monkeypatch.setattr(
        "ibkr_trader.signals.train.select_lgbm_params_optuna",
        lambda df, folds, *, seed, n_trials: (
            dict(DEFAULT_LGBM_PARAMS) | FAST_LGBM,
            search_record,
        ),
    )
    result = train_on_dataset(
        df,
        models_dir=tmp_path,
        universe=[f"S{i}" for i in range(1, 9)],
        window=WINDOW,
        seed=7,
        test_size=6,
        min_train=12,
        search="optuna",
        n_trials=3,
    )

    # artifact on disk: model + metadata + latest marker
    assert result.version == "v1"
    assert result.artifact_dir == tmp_path / "ml_lt" / "v1"
    assert (result.artifact_dir / MODEL_FILE).exists()
    assert (result.artifact_dir / RIDGE_FILE).exists()
    assert (tmp_path / "ml_lt" / "latest").read_text() == "v1"
    metadata = json.loads((result.artifact_dir / "metadata.json").read_text())
    assert metadata == result.metadata

    # metadata records what a saved model must know about itself
    assert metadata["feature_set_version"] == FEATURE_SET_VERSION
    assert metadata["eligibility"]["min_history_days"] == 252
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
    assert metadata["lgbm_search"] == search_record
    assert metadata["lgbm_grid_search"] is None
    assert metadata["ridge"]["model_file"] == RIDGE_FILE
    assert metadata["ridge"]["numeric_feature_columns"]
    assert "sector" not in metadata["ridge"]["numeric_feature_columns"]
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

    ridge = RidgeModel.load(
        result.artifact_dir / RIDGE_FILE,
        trained_sklearn_version=metadata["ridge"]["sklearn_version"],
    )
    ridge_predictions = ridge.predict(df[metadata["feature_columns"]])
    assert len(ridge_predictions) == len(df)
    assert all(math.isfinite(p) for p in ridge_predictions)

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
    assert second.metadata["lgbm_search"]["method"] == "explicit"
    assert second.metadata["lgbm_grid_search"] == second.metadata["lgbm_search"]
    assert (tmp_path / "ml_lt" / "latest").read_text() == "v2"
    assert load_latest_metadata(tmp_path)["version"] == "v2"
    assert (tmp_path / "ml_lt" / "v1" / MODEL_FILE).exists()  # old artifacts are kept


def test_ridge_artifact_round_trip_preserves_predictions(tmp_path: Path):
    df = _dataset().head(80)
    features = feature_columns(df)
    original = RidgeModel(seed=7)
    original.fit(df[features], df["label"])
    expected = original.predict(df[features])
    path = tmp_path / RIDGE_FILE
    original.save(path)

    from importlib import metadata as importlib_metadata

    loaded = RidgeModel.load(
        path, trained_sklearn_version=importlib_metadata.version("scikit-learn")
    )
    assert loaded.columns == original.columns
    assert loaded.predict(df[features]) == pytest.approx(expected)


def test_lgbm_capacity_grid_smoke_uses_fold_ic(tmp_path: Path):
    del tmp_path  # signature keeps the smoke fixture pattern consistent
    df = _dataset()
    folds = walk_forward_folds(sorted(df["date"].unique()), test_size=6, min_train=12)
    selected, record = select_lgbm_params(
        df,
        folds[:1],
        seed=7,
        grid={
            "num_leaves": (7, 15),
            "min_child_samples": (20,),
            "n_estimators": (20,),
        },
    )
    assert selected["num_leaves"] in {7, 15}
    assert len(record["candidates"]) == 2
    assert record["selection_metric"] == "mean fold rank IC"
    assert record["winner"] in record["candidates"]
    assert all("mean_fold_ic" in candidate for candidate in record["candidates"])


def test_load_latest_without_any_artifact_is_a_clear_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="train run"):
        load_latest_metadata(tmp_path)


def test_default_params_stay_light():
    assert LGBM_CAPACITY_GRID == {
        "num_leaves": (7, 15, 31),
        "min_child_samples": (20, 50, 100),
        "n_estimators": (100, 200, 400),
    }
    assert DEFAULT_LGBM_PARAMS["verbose"] == -1
    assert DEFAULT_LGBM_PARAMS["n_estimators"] == 100
    assert DEFAULT_LGBM_PARAMS["num_leaves"] == 7
    assert DEFAULT_LGBM_PARAMS["min_child_samples"] == 50
    assert "early_stopping_rounds" not in DEFAULT_LGBM_PARAMS
