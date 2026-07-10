"""Per-fold OOS backtest (ML-05): leakage guard, cash outside test spans, fold switching,
and an end-to-end smoke on a synthetic panel (that one skips without the [ml] extra)."""

import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.accounts import AccountType
from ibkr_trader.backtest.costs import RegisteredAccountCostModel
from ibkr_trader.backtest.engine import RegisteredStrategyConfig
from ibkr_trader.backtest.oos import (
    BASELINE_STRATEGIES,
    OOS_MODEL_VERSION,
    FoldModel,
    FoldSwitchingAllocator,
    run_oos_backtest,
)
from ibkr_trader.db.models import BacktestRun, Base, Instrument, PriceBar
from ibkr_trader.signals.eligibility import Candidate, EligibilityLimits

OPEN_LIMITS = EligibilityLimits(min_price=0.0, min_avg_dollar_volume=0.0, min_history_days=1)
CHEAP = RegisteredAccountCostModel(
    commission_per_share=0.0,
    min_commission=0.0,
    slippage_bps=0.0,
    spread_bps=0.0,
    churn_penalty_bps=0.0,
    fx_conversion_bps=0.0,
)
FEATURES = ["return_12m", "volatility"]


class StubModel:
    """SupervisedModel double: predicts the same rank for every row — 0.9 buys, 0.1 doesn't."""

    def __init__(self, value: float):
        self.value = value

    def fit(self, x: pd.DataFrame, y: pd.Series) -> None:  # pragma: no cover - unused
        pass

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return np.full(len(x), self.value)


def _fold_model(fold_id, train_end, test_start, test_end, value=0.9):
    return FoldModel(
        fold_id=fold_id,
        train_start=date(2019, 8, 30),
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
        model=StubModel(value),
    )


# two consecutive folds with the mandated 13-month train_end → test_start gap
FOLD_1 = {
    "train_end": date(2021, 7, 30),
    "test_start": date(2022, 8, 31),
    "test_end": date(2023, 1, 31),
}
FOLD_2 = {
    "train_end": date(2022, 1, 31),
    "test_start": date(2023, 2, 28),
    "test_end": date(2023, 7, 31),
}


def _candidates(ids):
    return [
        Candidate(
            instrument_id=iid,
            symbol=f"S{iid}",
            currency="CAD",
            exchange="TSX",
            last_price=100.0,
            avg_dollar_volume=5e6,
            history_days=500,
        )
        for iid in ids
    ]


def test_leakage_guard_refuses_a_model_whose_labels_reach_its_test_span():
    # train_end 2021-08-31 + 12-month horizon = 2022-08-31 — exactly the first test date, so
    # the last training labels are only realized AT the first decision: refused.
    with pytest.raises(ValueError, match="leak"):
        FoldSwitchingAllocator(
            "oos",
            [
                _fold_model(
                    0,
                    train_end=date(2021, 8, 31),
                    test_start=date(2022, 8, 31),
                    test_end=date(2023, 1, 31),
                )
            ],
            FEATURES,
        )


def test_returns_cash_outside_every_fold_test_span():
    allocator = FoldSwitchingAllocator(
        "oos", [_fold_model(0, **FOLD_1), _fold_model(1, **FOLD_2)], FEATURES
    )
    features = {1: {"return_12m": 0.3, "volatility": 0.2}}
    candidates = _candidates([1])

    allocator.asof(date(2022, 8, 30))  # the day before the first test date
    assert allocator.allocate(candidates, features) == {}
    allocator.asof(date(2023, 8, 1))  # past the last fold's test span
    assert allocator.allocate(candidates, features) == {}
    allocator.asof(date(2022, 8, 31))  # first decision date → invested
    weights = allocator.allocate(candidates, features)
    assert weights and math.isclose(sum(weights.values()), 0.20)  # capped single name


def test_each_decision_uses_its_own_folds_model_never_a_newer_one():
    # fold 0's stub scores above the median (buys); fold 1's below (stays in cash) — which
    # model answered is visible from whether the book is invested
    allocator = FoldSwitchingAllocator(
        "oos",
        [_fold_model(0, **FOLD_1, value=0.9), _fold_model(1, **FOLD_2, value=0.1)],
        FEATURES,
    )
    features = {1: {"return_12m": 0.3, "volatility": 0.2}}
    candidates = _candidates([1])

    allocator.asof(date(2022, 11, 1))  # inside fold 0's test span
    assert allocator.allocate(candidates, features)
    allocator.asof(date(2023, 3, 1))  # inside fold 1's test span
    assert allocator.allocate(candidates, features) == {}
    # between fold 0's last test date and fold 1's first: the OLDER model decides — fold 1's
    # training labels (through 2023-01-31) are not fully realized on 2023-02-01
    allocator.asof(date(2023, 2, 1))
    assert allocator.allocate(candidates, features)


def test_allocate_without_a_decision_date_raises():
    allocator = FoldSwitchingAllocator("oos", [_fold_model(0, **FOLD_1)], FEATURES)
    with pytest.raises(RuntimeError, match="asof"):
        allocator.allocate(_candidates([1]), {})


def _sqlite_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _seed_instrument(session, symbol, closes, *, sector=None):
    instrument = Instrument(symbol=symbol, exchange="TSX", currency="CAD", sector=sector)
    session.add(instrument)
    session.flush()
    for i, close in enumerate(closes):
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                ts=datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=i),
                bar_size="1 day",
                source="yahoo",
                what_to_show="ADJUSTED_LAST",
                open=close,
                high=close,
                low=close,
                close=close,
                volume=2_000_000.0,
            )
        )
    return instrument


def test_oos_backtest_end_to_end_smoke_on_synthetic_panel():
    pytest.importorskip("lightgbm")
    pytest.importorskip("sklearn")

    session = _sqlite_session()
    n = 1130  # ~37 months of bars → ~25 labeled monthly dates → 3 small folds
    sectors = ("Technology", "Financials", "Energy", None)
    for iid in range(1, 5):
        closes = [
            100.0 * math.exp(0.0002 * iid * i) * (1.0 + 0.05 * math.sin(i / (5.0 + iid)))
            for i in range(n)
        ]
        _seed_instrument(session, f"S{iid}", closes, sector=sectors[iid % len(sectors)])
    _seed_instrument(session, "XEQT", [50.0 * math.exp(0.0002 * i) for i in range(n)])
    session.commit()

    config = RegisteredStrategyConfig(account=AccountType.TFSA, eligibility=OPEN_LIMITS)
    oos = run_oos_backtest(
        session,
        ["S1", "S2", "S3", "S4", "XEQT"],
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2023, 3, 1, tzinfo=UTC),
        config=config,
        cost_model=CHEAP,
        seed=7,
        test_size=3,
        min_train=6,
        lgbm_params={"n_estimators": 40, "min_child_samples": 5},
        persist=True,
    )

    assert oos.n_folds >= 2
    assert {r.strategy for r in oos.results} == {"ml_lt_oos", "ml_lt_ridge_oos"} | set(
        BASELINE_STRATEGIES
    )
    # identical footing: every strategy's metrics start on the same first decision date and
    # stop at the stitched span's end
    first_days = {r.equity_curve[0][0] for r in oos.results}
    last_days = {r.equity_curve[-1][0] for r in oos.results}
    assert first_days == {oos.eval_start}
    assert last_days == {oos.test_end}
    for result in oos.results:
        assert result.params["eval_start"] == oos.eval_start.isoformat()
        assert result.params["oos_folds"] == oos.n_folds
        assert "universe_sha256_16" in result.params
    by_name = {r.strategy: r for r in oos.results}
    assert by_name["ml_lt_oos"].params["model_version"] == OOS_MODEL_VERSION
    assert by_name["ml_lt_ridge_oos"].params["model_version"] == OOS_MODEL_VERSION
    # all five persisted (the in-run buy-and-hold benchmark sub-simulations are not)
    assert session.scalar(select(func.count()).select_from(BacktestRun)) == 5
