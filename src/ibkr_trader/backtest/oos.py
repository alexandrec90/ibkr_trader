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
    LoadedUniverse,
    RegisteredStrategyConfig,
)
from ibkr_trader.backtest.universe import UniverseSpec
from ibkr_trader.db.models import Prediction
from ibkr_trader.signals.dataset import (
    LABEL_HORIZON_MONTHS,
    TRAINING_LIMITS,
    DatasetBuild,
    build_dataset,
    feature_columns,
)
from ibkr_trader.signals.eligibility import Candidate
from ibkr_trader.signals.portfolio import (
    Allocator,
    Features,
    Weights,
    get_allocator,
    normalize_weights,
)
from ibkr_trader.signals.train import LgbmModel, RidgeModel, _require_ml
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


def _training_dataset(
    session: Session,
    spec: UniverseSpec,
    window: tuple[datetime, datetime],
    config: RegisteredStrategyConfig,
) -> DatasetBuild:
    """The fold models' dataset: ``TRAINING_LIMITS`` history (young listings in) under the
    account's price and liquidity floors. Holding a young name is ``config.eligibility``'s
    call, in the simulation — learning from a fresh IPO and holding one are separate."""
    dataset = build_dataset(
        session,
        spec,
        *window,
        benchmark_symbol=config.benchmark_symbol,
        limits=replace(config.eligibility, min_history_days=TRAINING_LIMITS.min_history_days),
    )
    if dataset.frame.empty:
        raise ValueError("dataset is empty — ingest more history or widen the window")
    return dataset


@dataclass(frozen=True)
class _FoldFit:
    """Every model family's fold models over one dataset — fitted once, simulated per config."""

    folds: list[Fold]
    features: list[str]
    #: model name → (its fold models, their test-row predictions)
    models: dict[str, tuple[list[FoldModel], pd.DataFrame]]
    extra_params: dict[str, Any]

    @property
    def eval_start(self) -> date:
        return self.folds[0].test_dates[0]

    @property
    def test_end(self) -> date:
        return self.folds[-1].test_dates[-1]


def _fit_folds(
    df: pd.DataFrame,
    dataset: DatasetBuild,
    *,
    seed: int,
    test_size: int,
    min_train: int,
    lgbm_params: dict[str, Any] | None,
) -> _FoldFit:
    folds = walk_forward_folds(
        sorted(df["date"].unique()), test_size=test_size, min_train=min_train
    )
    features = feature_columns(df)
    factories: dict[str, Callable[[], SupervisedModel]] = {
        "ml_lt_oos": lambda: LgbmModel(lgbm_params, seed=seed),
        "ml_lt_ridge_oos": lambda: RidgeModel(seed=seed),
    }
    models = {}
    for name, factory in factories.items():
        fold_models = fit_fold_models(df, folds, factory)
        models[name] = (fold_models, build_oos_prediction_frame(df, folds, fold_models, features))
    extra_params = {
        "oos_folds": len(folds),
        "oos_test_span": [folds[0].test_dates[0].isoformat(), folds[-1].test_dates[-1].isoformat()],
        "universe_sha256_16": dataset.universe["sha256_16"],
        "oos_seed": seed,
        "oos_censored_rows": int(df["label_censored"].sum()),
    }
    return _FoldFit(folds, features, models, extra_params)


@dataclass(frozen=True)
class _Simulation:
    """What every account config's simulation shares: the panel, the bar window, the engine."""

    session: Session
    panel: LoadedUniverse
    window: tuple[datetime, datetime]
    engine: BacktestEngine
    baselines: Sequence[str]
    persist: bool

    def run(self, fit: _FoldFit, config: RegisteredStrategyConfig) -> OosBacktest:
        """Both model families and every baseline under one account config."""
        config = replace(config, eval_start=fit.eval_start)

        def run(allocator: Allocator, strategy_name: str | None = None) -> BacktestResult:
            return self.engine.run_allocator(
                allocator,
                self.panel,
                *self.window,
                strategy_name=strategy_name,
                config=config,
                session=self.session,
                persist=self.persist,
                extra_params=fit.extra_params,
            )

        results: list[BacktestResult] = []
        for name, (fold_models, predictions) in fit.models.items():
            result = run(FoldSwitchingAllocator(name, fold_models, fit.features))
            if self.persist:
                if result.run_id is None:  # defensive: persistence must provide provenance
                    raise RuntimeError("persisted OOS backtest did not expose its run id")
                persist_oos_predictions(self.session, result.run_id, name, predictions)
            results.append(result)
        results.extend(run(get_allocator(baseline), baseline) for baseline in self.baselines)
        return OosBacktest(
            eval_start=fit.eval_start,
            test_end=fit.test_end,
            n_folds=len(fit.folds),
            results=results,
        )


def run_oos_backtests(
    session: Session,
    universe: Sequence[str] | UniverseSpec,
    start: datetime,
    end: datetime,
    *,
    configs: Sequence[RegisteredStrategyConfig],
    sim_start: datetime | None = None,
    cost_model: RegisteredAccountCostModel | None = None,
    seed: int = 42,
    test_size: int = 6,
    min_train: int = 24,
    lgbm_params: dict[str, Any] | None = None,
    baselines: Sequence[str] = BASELINE_STRATEGIES,
    persist: bool = True,
) -> list[OosBacktest]:
    """The honest number: per-fold models + baselines over the stitched OOS test span, once
    per account config in ``configs`` — the fold models are fitted once and shared.

    ``start``/``end`` bound the dataset exactly like ``train run``; folds come from the same
    ``walk_forward_folds`` mechanics as training, so the stitched test span matches the
    recorded fold ICs. ``sim_start`` bounds the *simulation* bar load (default: ``start``) —
    pass a later date to respect data floors like USDCAD coverage; features only need ~12
    months of bars before ``eval_start``, and no allocation happens before it either way.
    All runs (both model families and every baseline) share the bar window and
    ``eval_start``, so they decide on identical dates. A ``UniverseSpec`` (the point-in-time
    S&P 500) is the universe of both the dataset and the simulation. The dataset takes the
    first config's price and liquidity floors; the configs may differ in what they *hold*
    (``eligibility.min_history_days``) — the holding-floor experiment — never in what the
    models learn from, which ``_training_dataset`` fixes at ``TRAINING_LIMITS``' history.
    """
    _require_ml()
    if not configs:
        raise ValueError("need at least one account config")
    spec = (
        universe
        if isinstance(universe, UniverseSpec)
        else UniverseSpec(tuple(symbol.upper() for symbol in universe))
    )
    dataset = _training_dataset(session, spec, (start, end), configs[0])
    fit = _fit_folds(
        dataset.frame,
        dataset,
        seed=seed,
        test_size=test_size,
        min_train=min_train,
        lgbm_params=lgbm_params,
    )
    # decisions stop at the last test date, so the simulation must too — a cash tail (models)
    # or extra trading months (baselines) past it would skew every stitched-span metric
    window = (sim_start or start, datetime.combine(fit.test_end, datetime.max.time(), tzinfo=UTC))
    simulation = _Simulation(
        session,
        # one load shared by every run, so models and baselines see the identical panel
        spec.load(session, window, measure_from=fit.eval_start),
        window,
        BacktestEngine(cost_model=cost_model),
        baselines,
        persist,
    )
    return [simulation.run(fit, config) for config in configs]


def run_oos_backtest(
    session: Session,
    universe: Sequence[str] | UniverseSpec,
    start: datetime,
    end: datetime,
    *,
    config: RegisteredStrategyConfig | None = None,
    **kwargs: Any,
) -> OosBacktest:
    """``run_oos_backtests`` under one account config (default: ``RegisteredStrategyConfig()``)."""
    configs = (config or RegisteredStrategyConfig(),)
    return run_oos_backtests(session, universe, start, end, configs=configs, **kwargs)[0]
