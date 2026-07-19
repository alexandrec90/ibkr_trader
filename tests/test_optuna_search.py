"""Optuna LightGBM capacity-search behavior (TOOLS-04)."""

from datetime import date

import pandas as pd
import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("sklearn")
pytest.importorskip("optuna")

from ibkr_trader.signals import train as train_module  # noqa: E402
from ibkr_trader.signals.train import (  # noqa: E402
    LGBM_CAPACITY_GRID,
    LGBM_OPTUNA_SEARCH_SPACE,
    select_lgbm_params,
    select_lgbm_params_optuna,
)
from ibkr_trader.signals.validation import Fold, FoldResult  # noqa: E402


def _folds(count: int = 4) -> list[Fold]:
    return [
        Fold(
            fold_id=index,
            train_dates=(date(2020, 1, 31),),
            test_dates=(date(2021, index + 1, 28),),
        )
        for index in range(count)
    ]


def _synthetic_panel() -> tuple[pd.DataFrame, list[Fold]]:
    dates = [date(2020, month, 28) for month in range(1, 7)]
    rows = [
        {
            "instrument_id": instrument_id,
            "symbol": f"S{instrument_id}",
            "date": rebalance_date,
            "fwd_excess_return": instrument_id / 100,
            "label": instrument_id / 8,
            "signal": float(instrument_id) + rebalance_date.month / 100,
        }
        for rebalance_date in dates
        for instrument_id in range(1, 9)
    ]
    folds = [
        Fold(fold_id=0, train_dates=tuple(dates[:3]), test_dates=(dates[3],)),
        Fold(fold_id=1, train_dates=tuple(dates[:4]), test_dates=(dates[4],)),
    ]
    return pd.DataFrame(rows), folds


def test_optuna_search_repeats_real_lightgbm_result_on_synthetic_panel():
    frame, folds = _synthetic_panel()
    small_space = {
        "num_leaves": (3, 7),
        "min_child_samples": (2,),
        "n_estimators": (10,),
    }

    first, first_record = select_lgbm_params_optuna(
        frame, folds[:1], seed=11, n_trials=1, search_space=small_space
    )
    second, second_record = select_lgbm_params_optuna(
        frame, folds[:1], seed=11, n_trials=1, search_space=small_space
    )

    assert first == second
    assert first_record["best_trial"]["mean_fold_ic"] == pytest.approx(
        second_record["best_trial"]["mean_fold_ic"]
    )
    assert first_record["best_trial"]["mean_fold_ic"] is not None


def test_optuna_search_is_deterministic_and_trials_stay_in_bounds(monkeypatch):
    def score(df, fold, params, *, seed):
        del df, seed
        return (
            0.2
            - abs(params["num_leaves"] - 17) / 100
            - abs(params["min_child_samples"] - 45) / 1000
            - abs(params["n_estimators"] - 220) / 10000
            + fold.fold_id / 10000
        )

    monkeypatch.setattr(train_module, "_evaluate_lgbm_fold", score)
    frame = pd.DataFrame({"synthetic": [1.0]})
    first, first_record = select_lgbm_params_optuna(frame, _folds(), seed=19, n_trials=16)
    second, second_record = select_lgbm_params_optuna(frame, _folds(), seed=19, n_trials=16)

    assert first == second
    assert [trial["params"] for trial in first_record["trials"]] == [
        trial["params"] for trial in second_record["trials"]
    ]
    assert first_record["sampler"] == "TPESampler"
    assert first_record["seed"] == 19
    assert first_record["n_trials"] == 16
    assert first_record["completed_count"] + first_record["pruned_count"] == 16
    for trial in first_record["trials"]:
        for name, allowed in LGBM_OPTUNA_SEARCH_SPACE.items():
            assert trial["params"][name] in allowed


def test_median_pruner_stops_some_clearly_bad_candidates(monkeypatch):
    def rigged_score(df, fold, params, *, seed):
        del df, seed
        base = -1.0 if params["num_leaves"] >= 10 else 1.0
        return base + fold.fold_id / 1000

    monkeypatch.setattr(train_module, "_evaluate_lgbm_fold", rigged_score)
    _, record = select_lgbm_params_optuna(
        pd.DataFrame({"rigged": [1]}),
        _folds(5),
        seed=7,
        n_trials=30,
        search_space={
            "num_leaves": range(7, 13),
            "min_child_samples": (20,),
            "n_estimators": (100,),
        },
    )

    assert record["pruned_count"] > 0
    assert any(trial["state"] == "pruned" for trial in record["trials"])
    assert all(
        len(trial["fold_ics"]) >= 2 for trial in record["trials"] if trial["state"] == "pruned"
    )


def test_optuna_exact_space_winner_matches_grid_best_ic(monkeypatch):
    def fake_evaluate(df, folds, factories):
        del df
        name, factory = next(iter(factories.items()))
        params = factory.args[0]
        score = (
            params["num_leaves"] / 100
            - params["min_child_samples"] / 1000
            + params["n_estimators"] / 10000
        )
        return [
            FoldResult(
                fold_id=fold.fold_id,
                train_start=fold.train_dates[0],
                train_end=fold.train_dates[-1],
                test_start=fold.test_dates[0],
                test_end=fold.test_dates[-1],
                n_train=10,
                n_test=5,
                ics_by_model={name: [score]},
            )
            for fold in folds
        ]

    monkeypatch.setattr(train_module, "evaluate_walk_forward", fake_evaluate)
    exact_space = {
        "num_leaves": LGBM_CAPACITY_GRID["num_leaves"][:2],
        "min_child_samples": LGBM_CAPACITY_GRID["min_child_samples"][:2],
        "n_estimators": LGBM_CAPACITY_GRID["n_estimators"][:2],
    }
    frame = pd.DataFrame({"synthetic": [1.0]})
    _, grid_record = select_lgbm_params(frame, _folds(2), seed=5, grid=exact_space)
    _, optuna_record = select_lgbm_params_optuna(
        frame,
        _folds(2),
        seed=5,
        n_trials=8,
        search_space=exact_space,
    )

    assert optuna_record["best_trial"]["mean_fold_ic"] == pytest.approx(
        grid_record["winner"]["mean_fold_ic"]
    )
    assert optuna_record["best_trial"]["params"] in [
        candidate["params"]
        for candidate in grid_record["candidates"]
        if candidate["mean_fold_ic"] == pytest.approx(grid_record["winner"]["mean_fold_ic"])
    ]


@pytest.mark.parametrize(
    ("search_space", "message"),
    [
        ({"num_leaves": (7,)}, "contain exactly"),
        (
            {
                "num_leaves": (7, "wide"),
                "min_child_samples": (20,),
                "n_estimators": (100,),
            },
            "must contain integers",
        ),
        (
            {
                "num_leaves": (7, 7),
                "min_child_samples": (20,),
                "n_estimators": (100,),
            },
            "duplicate values",
        ),
    ],
)
def test_optuna_search_rejects_invalid_spaces(search_space, message):
    with pytest.raises(ValueError, match=message):
        select_lgbm_params_optuna(
            pd.DataFrame({"synthetic": [1.0]}),
            _folds(2),
            seed=1,
            n_trials=1,
            search_space=search_space,
        )


def test_optuna_search_rejects_invalid_trial_and_fold_counts():
    frame = pd.DataFrame({"synthetic": [1.0]})
    with pytest.raises(ValueError, match="n_trials"):
        select_lgbm_params_optuna(frame, _folds(2), seed=1, n_trials=0)
    with pytest.raises(ValueError, match="at least one"):
        select_lgbm_params_optuna(frame, [], seed=1, n_trials=1)
