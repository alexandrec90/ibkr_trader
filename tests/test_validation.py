"""Walk-forward validation contract (ML-03): the 12-month purge means no training row's
forward window ever overlaps a test date; expanding windows; Spearman rank IC scoring."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from ibkr_trader.signals.dataset import LABEL_HORIZON_MONTHS
from ibkr_trader.signals.validation import (
    evaluate_walk_forward,
    rank_ic,
    summarize_ics,
    walk_forward_folds,
)


def _monthly_dates(n: int, start_year: int = 2018) -> list[date]:
    return [date(start_year + (m - 1) // 12, (m - 1) % 12 + 1, 28) for m in range(1, n + 1)]


def _month_index(d: date) -> int:
    return d.year * 12 + d.month


# --- purge correctness (the plan's headline test) ---------------------------------------------


def test_no_training_forward_window_overlaps_any_test_date():
    dates = _monthly_dates(60)
    folds = walk_forward_folds(dates, test_size=6, min_train=24)
    assert folds
    for fold in folds:
        test_start = min(fold.test_dates)
        for train_date in fold.train_dates:
            # a training row at t carries a label built from returns over (t, t+12m]
            forward_end = _month_index(train_date) + LABEL_HORIZON_MONTHS
            assert forward_end < _month_index(test_start), (
                f"fold {fold.fold_id}: train row {train_date} forward window reaches the "
                f"test block starting {test_start}"
            )


def test_purge_below_the_label_horizon_is_rejected():
    with pytest.raises(ValueError, match="label horizon"):
        walk_forward_folds(_monthly_dates(60), purge=LABEL_HORIZON_MONTHS - 1)


# --- fold structure ---------------------------------------------------------------------------


def test_folds_are_expanding_and_test_blocks_partition_the_tail():
    dates = _monthly_dates(60)
    folds = walk_forward_folds(dates, test_size=6, min_train=24)
    assert len(folds) == 4  # test blocks at indices 36, 42, 48, 54
    for previous, current in zip(folds, folds[1:], strict=False):
        assert set(previous.train_dates) < set(current.train_dates)  # expanding
        assert max(previous.test_dates) < min(current.test_dates)  # ordered
        assert not set(previous.test_dates) & set(current.test_dates)  # disjoint
    covered = sorted(d for fold in folds for d in fold.test_dates)
    assert covered == dates[36:]  # min_train + purge = 36; every later date is tested once


def test_too_short_history_raises():
    with pytest.raises(ValueError, match="rebalance dates"):
        walk_forward_folds(_monthly_dates(30), test_size=6, min_train=24)


# --- rank IC ----------------------------------------------------------------------------------


def test_rank_ic_is_1_for_monotone_and_minus_1_for_reversed():
    predicted = pd.Series([0.1, 0.5, 0.7, 0.9])
    actual = pd.Series([10.0, 20.0, 30.0, 40.0])
    assert rank_ic(predicted, actual) == pytest.approx(1.0)
    assert rank_ic(predicted, actual.iloc[::-1].reset_index(drop=True)) == pytest.approx(-1.0)


def test_rank_ic_degenerate_cross_sections_return_none():
    assert rank_ic(pd.Series([1.0, 2.0]), pd.Series([1.0, 2.0])) is None  # too small
    assert rank_ic(pd.Series([1.0, 1.0, 1.0]), pd.Series([1.0, 2.0, 3.0])) is None  # constant


# --- evaluation loop --------------------------------------------------------------------------


class _OracleModel:
    """Predicts the `signal` feature verbatim — no ML deps needed to test the loop."""

    def fit(self, x: pd.DataFrame, y: pd.Series) -> None:
        self.fitted_rows = len(x)

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return x["signal"].to_numpy()


def _toy_frame(dates: list[date], n_names: int = 5) -> pd.DataFrame:
    rows = []
    for day in dates:
        for iid in range(n_names):
            rows.append(
                {
                    "instrument_id": iid,
                    "symbol": f"S{iid}",
                    "date": day,
                    "signal": float(iid),
                    "fwd_excess_return": float(iid),
                    "label": iid / (n_names - 1),
                }
            )
    return pd.DataFrame(rows)


def test_evaluate_walk_forward_scores_each_test_date_and_fresh_models_per_fold():
    dates = _monthly_dates(60)
    df = _toy_frame(dates)
    folds = walk_forward_folds(dates, test_size=6, min_train=24)
    results = evaluate_walk_forward(df, folds, {"oracle": _OracleModel})

    assert [r.fold_id for r in results] == [f.fold_id for f in folds]
    for result, fold in zip(results, folds, strict=True):
        assert result.n_test == len(fold.test_dates) * 5
        assert result.ics_by_model["oracle"] == pytest.approx([1.0] * len(fold.test_dates))
        assert result.ic_mean("oracle") == pytest.approx(1.0)
        assert result.ic_std("oracle") == pytest.approx(0.0)
    overall = summarize_ics(results, "oracle")
    assert overall["mean"] == pytest.approx(1.0)
    assert overall["n_dates"] == sum(len(f.test_dates) for f in folds)
