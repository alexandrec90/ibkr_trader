"""Walk-forward validation for the long-term model (ML-03).

Expanding-window folds over monthly rebalance dates with a **purge gap** of at least the label
horizon (12 months) between each train window's end and its test window's start: a training row
at date t carries a label built from returns through t+12m, so without the purge the last year
of training rows would overlap — and leak — the test period. No shuffled/random splits, ever.

The fold metric is the Spearman rank IC between predicted and realized label per test date
(cross-sectional), reported as mean ± std across dates. Model fitting lives in signals.train;
this module only slices time and scores predictions, so it works without the ``[ml]`` extra.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

import numpy as np
import pandas as pd

from ibkr_trader.signals.dataset import LABEL_HORIZON_MONTHS, feature_columns

#: Fewest names a test date needs for a rank correlation to mean anything.
_MIN_CROSS_SECTION = 3


class SupervisedModel(Protocol):
    """What a model must offer: fit on a feature frame + label, predict scores for one."""

    def fit(self, x: pd.DataFrame, y: pd.Series) -> None: ...

    def predict(self, x: pd.DataFrame) -> np.ndarray: ...


@dataclass(frozen=True)
class Fold:
    """One expanding-window fold: train on everything up to the purge gap, test one block."""

    fold_id: int
    train_dates: tuple[date, ...]
    test_dates: tuple[date, ...]


def walk_forward_folds(
    dates: Sequence[date],
    *,
    test_size: int = 6,
    purge: int = LABEL_HORIZON_MONTHS,
    min_train: int = 24,
) -> list[Fold]:
    """Expanding-window folds over sorted unique rebalance ``dates``.

    Fold k tests the block of ``test_size`` dates starting at index ``min_train + purge +
    k*test_size`` and trains on all dates strictly before ``test_start - purge`` — with
    monthly dates and ``purge >= LABEL_HORIZON_MONTHS``, no training row's 12-month forward
    window reaches any test date. Raises ValueError when the history is too short for even
    one fold (or when the purge is weaker than the label horizon).
    """
    if purge < LABEL_HORIZON_MONTHS:
        raise ValueError(
            f"purge={purge} months is below the label horizon "
            f"({LABEL_HORIZON_MONTHS}m) — training labels would leak into tests"
        )
    if test_size < 1 or min_train < 1:
        raise ValueError("test_size and min_train must be >= 1")
    ordered = sorted(set(dates))
    folds: list[Fold] = []
    for test_start in range(min_train + purge, len(ordered), test_size):
        train = tuple(ordered[: test_start - purge])
        test = tuple(ordered[test_start : test_start + test_size])
        if train and test:
            folds.append(Fold(fold_id=len(folds), train_dates=train, test_dates=test))
    if not folds:
        raise ValueError(
            f"only {len(ordered)} rebalance dates — need more than "
            f"{min_train + purge} (min_train={min_train} + purge={purge}) for one fold"
        )
    return folds


def rank_ic(predicted: pd.Series, actual: pd.Series) -> float | None:
    """Spearman rank correlation; None when the cross-section is too small or constant."""
    paired = pd.concat({"predicted": predicted, "actual": actual}, axis=1).dropna()
    if len(paired) < _MIN_CROSS_SECTION:
        return None
    if paired["predicted"].nunique() < 2 or paired["actual"].nunique() < 2:
        return None
    ranks = paired.rank(method="average")
    value = ranks["predicted"].corr(ranks["actual"])
    return None if pd.isna(value) else float(value)


@dataclass
class FoldResult:
    """One fold's outcome: per-test-date rank ICs for every evaluated model."""

    fold_id: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    n_train: int
    n_test: int
    ics_by_model: dict[str, list[float]] = field(default_factory=dict)

    def ic_mean(self, model: str) -> float | None:
        ics = self.ics_by_model.get(model) or []
        return float(np.mean(ics)) if ics else None

    def ic_std(self, model: str) -> float | None:
        ics = self.ics_by_model.get(model) or []
        return float(np.std(ics)) if ics else None


def summarize_ics(results: Sequence[FoldResult], model: str) -> dict[str, float | int | None]:
    """Pool every test date's IC across folds: the headline mean ± std for one model."""
    pooled = [ic for result in results for ic in result.ics_by_model.get(model, [])]
    return {
        "mean": float(np.mean(pooled)) if pooled else None,
        "std": float(np.std(pooled)) if pooled else None,
        "n_dates": len(pooled),
    }


def evaluate_walk_forward(
    df: pd.DataFrame,
    folds: Sequence[Fold],
    model_factories: Mapping[str, Callable[[], SupervisedModel]],
    *,
    label_column: str = "label",
) -> list[FoldResult]:
    """Fit each model per fold on train dates, score rank IC per test date.

    ``df`` is a dataset frame from signals.dataset; models receive exactly its feature
    columns (each model handles its own encoding/imputation). A fresh model instance is
    built per (fold, model) so no state crosses folds.
    """
    features = feature_columns(df)
    results: list[FoldResult] = []
    for fold in folds:
        train = df[df["date"].isin(fold.train_dates)]
        test = df[df["date"].isin(fold.test_dates)]
        result = FoldResult(
            fold_id=fold.fold_id,
            train_start=fold.train_dates[0],
            train_end=fold.train_dates[-1],
            test_start=fold.test_dates[0],
            test_end=fold.test_dates[-1],
            n_train=len(train),
            n_test=len(test),
        )
        if not train.empty and not test.empty:
            for name, factory in model_factories.items():
                model = factory()
                model.fit(train[features], train[label_column])
                predicted = pd.Series(model.predict(test[features]), index=test.index)
                ics = [
                    ic
                    for _, block in test.groupby("date")
                    if (ic := rank_ic(predicted.loc[block.index], block[label_column])) is not None
                ]
                result.ics_by_model[name] = ics
        results.append(result)
    return results
