"""Per-fold out-of-sample backtest (ML-05) — the honest after-cost number.

The leaderboard run of a deployed artifact is in-sample: the final model trains on every
labeled row, so backtesting it over its own training window proves nothing (see the ML-04
verdict). This module produces the number the promotion rule actually wants: one simulated
P&L over the stitched walk-forward test span where **every decision at date t comes from a
model whose training labels were fully realized before t**.

Mechanics: build the supervised dataset (signals.dataset), slice it into purged walk-forward
folds (signals.validation), fit one model per fold **in memory** (no artifacts — these models
exist only to answer "what would I honestly have known at t?"), and hand them to a
``FoldSwitchingAllocator`` that routes each rebalance decision to the fold whose test window
covers it — cash before the first test date and after the last one. Baselines run through the
exact same engine, bar window and ``eval_start`` so all strategies decide on identical dates.

Reads Postgres only (through signals.dataset / the engine loaders); model fitting needs the
``[ml]`` extra, but the allocator itself is model-agnostic so tests drive it with stubs.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from ibkr_trader.backtest.costs import RegisteredAccountCostModel
from ibkr_trader.backtest.engine import (
    BacktestEngine,
    BacktestResult,
    RegisteredStrategyConfig,
)
from ibkr_trader.db.models import Prediction
from ibkr_trader.signals.dataset import LABEL_HORIZON_MONTHS, build_dataset, feature_columns
from ibkr_trader.signals.eligibility import Candidate
from ibkr_trader.signals.portfolio import (
    Allocator,
    Features,
    Weights,
    get_allocator,
    normalize_weights,
)
from ibkr_trader.signals.train import LgbmModel, RidgeModel, _require_ml, _universe_hash
from ibkr_trader.signals.validation import Fold, SupervisedModel, walk_forward_folds

#: ``params["model_version"]`` on every per-fold run — no artifact version applies, because
#: no single artifact made these decisions; the version *is* the validation scheme.
OOS_MODEL_VERSION = "oos-walkforward"

#: Baselines run over the identical decision dates so the comparison is fair (deliverable 4).
BASELINE_STRATEGIES = ("momentum_lt", "equal_weight", "buy_and_hold")


def _month_index(day: date) -> int:
    return day.year * 12 + day.month


@dataclass(frozen=True)
class FoldModel:
    """One fold's fitted model plus the spans proving it never saw its own test months."""

    fold_id: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    model: SupervisedModel


class FoldSwitchingAllocator(Allocator):
    """Route each rebalance decision to the fold model whose test window covers it.

    Unregistered (like ``ScoreAllocator``) — it needs fitted fold models, so it is built
    explicitly by ``run_oos_backtest``. The engine feeds the decision date via ``asof``;
    a decision at t uses the latest fold with ``test_start <= t`` (between two folds' test
    blocks that is the *older* model — never a newer one whose labels could reach t), and
    dates before the first test date or after the last test span get no allocation: cash.

    Leakage guard: construction and every decision re-check that the chosen model's
    ``train_end`` plus the label horizon lies strictly before the decision date, so a
    mis-built mapping fails loudly instead of quietly backtesting the future.
    """

    version = OOS_MODEL_VERSION
    max_names = 15
    max_weight = 0.20

    def __init__(
        self,
        name: str,
        fold_models: Sequence[FoldModel],
        feature_cols: Sequence[str],
        *,
        horizon_months: int = LABEL_HORIZON_MONTHS,
    ):
        if not fold_models:
            raise ValueError("need at least one fold model")
        self.name = name
        self._folds = sorted(fold_models, key=lambda fm: fm.test_start)
        self._feature_columns = list(feature_cols)
        self._horizon_months = horizon_months
        self._day: date | None = None
        for fm in self._folds:
            self._assert_no_leak(fm, fm.test_start)

    def asof(self, day: date) -> None:
        self._day = day

    def _assert_no_leak(self, fm: FoldModel, day: date) -> None:
        # a training row at train_end carries a label built from returns through
        # train_end + horizon; the gap to the decision date must exceed the horizon
        if _month_index(day) - _month_index(fm.train_end) <= self._horizon_months:
            raise ValueError(
                f"leakage: fold {fm.fold_id} trained through {fm.train_end}, whose labels "
                f"consume returns up to {self._horizon_months} months later — that reaches "
                f"the decision date {day}"
            )

    def _fold_for(self, day: date) -> FoldModel | None:
        if day > self._folds[-1].test_end:
            return None
        chosen = None
        for fm in self._folds:
            if fm.test_start <= day:
                chosen = fm
        if chosen is not None:
            self._assert_no_leak(chosen, day)
        return chosen

    def allocate(self, candidates: Sequence[Candidate], features: Features) -> Weights:
        if self._day is None:
            raise RuntimeError("allocate() before asof() — the engine sets the decision date")
        fold = self._fold_for(self._day)
        if fold is None:
            return {}  # outside every fold's test span → hold cash
        ids = [c.instrument_id for c in candidates]
        if not ids:
            return {}
        nan = float("nan")
        x = pd.DataFrame(
            [
                {col: features.get(iid, {}).get(col, nan) for col in self._feature_columns}
                for iid in ids
            ]
        )
        predicted = fold.model.predict(x)
        scores: dict[int, float] = {}
        for iid, value in zip(ids, predicted, strict=True):
            score = float(value) - 0.5  # predicted rank centered: above-median → positive
            if score > 0 and math.isfinite(score):
                scores[iid] = score
        return normalize_weights(scores, max_names=self.max_names, max_weight=self.max_weight)


def fit_fold_models(
    df: pd.DataFrame,
    folds: Sequence[Fold],
    factory: Callable[[], SupervisedModel],
) -> list[FoldModel]:
    """Fit one fresh model per fold on that fold's training dates only (in memory)."""
    features = feature_columns(df)
    fold_models: list[FoldModel] = []
    for fold in folds:
        train = df[df["date"].isin(fold.train_dates)]
        if train.empty:
            raise ValueError(f"fold {fold.fold_id} has no training rows")
        model = factory()
        model.fit(train[features], train["label"])
        fold_models.append(
            FoldModel(
                fold_id=fold.fold_id,
                train_start=fold.train_dates[0],
                train_end=fold.train_dates[-1],
                test_start=fold.test_dates[0],
                test_end=fold.test_dates[-1],
                model=model,
            )
        )
    return fold_models


def build_oos_prediction_frame(
    df: pd.DataFrame,
    folds: Sequence[Fold],
    fold_models: Sequence[FoldModel],
    feature_cols: Sequence[str],
) -> pd.DataFrame:
    """Predict each fold's test rows once, preserving the fold provenance.

    This is the reusable signal source for factor research. It deliberately consumes the
    already-fitted fold models from the OOS run; report code never fits or reloads a model.
    """
    models_by_id = {fold.fold_id: fold for fold in fold_models}
    rows: list[pd.DataFrame] = []
    for fold in folds:
        fitted = models_by_id.get(fold.fold_id)
        if fitted is None:
            raise ValueError(f"missing fitted model for fold {fold.fold_id}")
        test = df[df["date"].isin(fold.test_dates)].copy()
        if test.empty:
            raise ValueError(f"fold {fold.fold_id} has no test rows")
        predicted = fitted.model.predict(test[list(feature_cols)])
        if len(predicted) != len(test):
            raise ValueError(
                f"fold {fold.fold_id} returned {len(predicted)} predictions for {len(test)} rows"
            )
        test["score"] = predicted
        test["fold_id"] = fold.fold_id
        rows.append(test[["instrument_id", "symbol", "date", "score", "fold_id"]])
    result = pd.concat(rows, ignore_index=True)
    if result[["instrument_id", "date"]].duplicated().any():
        raise ValueError("OOS folds produced duplicate instrument/date predictions")
    return result


def persist_oos_predictions(
    session: Session,
    run_id: int,
    model_name: str,
    predictions: pd.DataFrame,
) -> None:
    """Attach auditable fold predictions to one persisted OOS backtest run."""
    created_at = datetime.now(tz=UTC)
    session.add_all(
        [
            Prediction(
                model_name=model_name,
                model_version=OOS_MODEL_VERSION,
                backtest_run_id=run_id,
                instrument_id=int(row.instrument_id),
                ts=datetime.combine(row.date, datetime.min.time(), tzinfo=UTC),
                horizon="12m",
                score=float(row.score),
                features={"fold_id": int(row.fold_id), "signal_source": "oos-fold"},
                created_at=created_at,
            )
            for row in predictions.itertuples(index=False)
        ]
    )
    session.flush()


@dataclass
class OosBacktest:
    """One `backtest oos` invocation: the stitched OOS span and all five strategies' runs."""

    eval_start: date  # first fold's first test date — the first decision date
    test_end: date  # last fold's last test date — decisions stop here
    n_folds: int
    results: list[BacktestResult]


def run_oos_backtest(
    session: Session,
    universe: Sequence[str],
    start: datetime,
    end: datetime,
    *,
    sim_start: datetime | None = None,
    config: RegisteredStrategyConfig | None = None,
    cost_model: RegisteredAccountCostModel | None = None,
    seed: int = 42,
    test_size: int = 6,
    min_train: int = 24,
    lgbm_params: dict[str, Any] | None = None,
    baselines: Sequence[str] = BASELINE_STRATEGIES,
    persist: bool = True,
) -> OosBacktest:
    """The honest number: per-fold models + baselines over the stitched OOS test span.

    ``start``/``end`` bound the dataset exactly like ``train run``; folds come from the same
    ``walk_forward_folds`` mechanics as training, so the stitched test span matches the
    recorded fold ICs. ``sim_start`` bounds the *simulation* bar load (default: ``start``) —
    pass a later date to respect data floors like USDCAD coverage; features only need ~12
    months of bars before ``eval_start``, and no allocation happens before it either way.
    All runs (both model families and every baseline) share the bar window, the account
    config and ``eval_start``, so they decide on identical dates.
    """
    _require_ml()
    config = config or RegisteredStrategyConfig()
    df = build_dataset(
        session,
        universe,
        start,
        end,
        benchmark_symbol=config.benchmark_symbol,
        limits=config.eligibility,
    )
    if df.empty:
        raise ValueError("dataset is empty — ingest more history or widen the window")
    folds = walk_forward_folds(
        sorted(df["date"].unique()), test_size=test_size, min_train=min_train
    )
    features = feature_columns(df)
    eval_start = folds[0].test_dates[0]
    test_end = folds[-1].test_dates[-1]
    config = replace(config, eval_start=eval_start)

    sim_start = sim_start or start
    # decisions stop at the last test date, so the simulation must too — a cash tail (models)
    # or extra trading months (baselines) past it would skew every stitched-span metric
    sim_end = datetime.combine(test_end, datetime.max.time(), tzinfo=UTC)
    symbols = [symbol.upper() for symbol in universe]
    extra_params = {
        "oos_folds": len(folds),
        "oos_test_span": [eval_start.isoformat(), test_end.isoformat()],
        "universe_sha256_16": _universe_hash(universe),
        "oos_seed": seed,
    }

    factories: dict[str, Callable[[], SupervisedModel]] = {
        "ml_lt_oos": lambda: LgbmModel(lgbm_params, seed=seed),
        "ml_lt_ridge_oos": lambda: RidgeModel(seed=seed),
    }
    engine = BacktestEngine(cost_model=cost_model)
    results: list[BacktestResult] = []
    for name, factory in factories.items():
        fold_models = fit_fold_models(df, folds, factory)
        predictions = build_oos_prediction_frame(df, folds, fold_models, features)
        allocator = FoldSwitchingAllocator(name, fold_models, features)
        result = engine.run_allocator(
            allocator,
            symbols,
            sim_start,
            sim_end,
            config=config,
            session=session,
            persist=persist,
            extra_params=extra_params,
        )
        if persist:
            if result.run_id is None:  # defensive: engine persistence must provide provenance
                raise RuntimeError("persisted OOS backtest did not expose its run id")
            persist_oos_predictions(session, result.run_id, name, predictions)
        results.append(result)
    for baseline in baselines:
        results.append(
            engine.run_allocator(
                get_allocator(baseline),
                symbols,
                sim_start,
                sim_end,
                strategy_name=baseline,
                config=config,
                session=session,
                persist=persist,
                extra_params=extra_params,
            )
        )
    return OosBacktest(
        eval_start=eval_start, test_end=test_end, n_folds=len(folds), results=results
    )
