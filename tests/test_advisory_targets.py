"""Strategy targets from the forward snapshots: in-memory SQLite, no network."""

import math
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ibkr_trader.advisory.targets import (
    StrategyTargets,
    TargetWeight,
    daily_volatility,
    latest_targets,
    questrade_candidates,
)
from ibkr_trader.db.models import Base, Instrument, PriceBar, StrategySnapshot

TODAY = date(2026, 10, 18)


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as value:
        yield value


def _instrument(session: Session, symbol: str, currency: str, asset_class: str = "STK"):
    row = Instrument(
        symbol=symbol,
        exchange="TSX" if currency == "CAD" else "SMART",
        currency=currency,
        sec_type="STK",
        name=symbol,
        asset_class=asset_class,
        leveraged=False,
    )
    session.add(row)
    session.flush()
    return row


def _bars(session: Session, instrument: Instrument, closes: list[float]) -> None:
    for offset, close in enumerate(reversed(closes)):
        day = TODAY - timedelta(days=offset)
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                ts=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                bar_size="1 day",
                source="yahoo",
                what_to_show="ADJUSTED_LAST",
                open=close,
                high=close,
                low=close,
                close=close,
                volume=1_000_000,
            )
        )


def _snapshot(session: Session, strategy: str, day: date, weights: dict) -> None:
    session.add(
        StrategySnapshot(
            strategy=strategy,
            model_version="v1",
            feature_set_version="f1",
            ts=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            weights={str(k): v for k, v in weights.items()},
            params={},
            created_at=datetime.now(tz=UTC),
        )
    )
    session.flush()


def test_daily_volatility_is_the_sample_stdev_of_log_returns():
    closes = [100.0 * (1.01 if i % 2 else 0.99) ** 1 for i in range(61)]
    returns = [math.log(b / a) for a, b in pairwise(closes)]
    mean = sum(returns) / len(returns)
    expected = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    assert daily_volatility(closes) == pytest.approx(expected)
    assert daily_volatility(closes[:10]) is None  # too few returns to mean anything


def test_questrade_candidates_follow_its_documented_symbology():
    assert questrade_candidates("XEQT", "CAD") == ["XEQT.TO"]
    assert questrade_candidates("BBD-B", "CAD") == ["BBD.B.TO"]
    assert questrade_candidates("AAPL", "USD") == ["AAPL"]
    assert questrade_candidates("BRK-B", "USD") == ["BRK-B", "BRK.B"]


def test_latest_targets_reads_the_newest_snapshot_with_volatility(session: Session):
    xic = _instrument(session, "XIC", "CAD", asset_class="ETF")
    bank = _instrument(session, "RY", "CAD")
    _bars(session, bank, [100.0 + (i % 3) for i in range(80)])
    _snapshot(session, "core", TODAY - timedelta(days=40), {xic.id: 1.0})
    _snapshot(session, "core", TODAY - timedelta(days=5), {xic.id: 0.7, bank.id: 0.3, 999: 0.1})
    _snapshot(session, "other", TODAY, {xic.id: 1.0})

    targets = latest_targets(session, "core", today=TODAY)

    assert targets.asof == TODAY - timedelta(days=5)
    assert [w.symbol for w in targets.weights] == ["XIC", "RY"]
    xic_weight, bank_weight = targets.weights
    assert xic_weight.broad_etf and xic_weight.daily_vol is None  # no bars
    assert not bank_weight.broad_etf and bank_weight.daily_vol and bank_weight.daily_vol > 0
    assert targets.missing == ("999",)
    assert isinstance(targets, StrategyTargets) and isinstance(xic_weight, TargetWeight)


def test_latest_targets_drops_zero_weights(session: Session):
    xic = _instrument(session, "XIC", "CAD")
    spy = _instrument(session, "SPY", "USD")
    _snapshot(session, "core", TODAY, {xic.id: 1.0, spy.id: 0.0})
    targets = latest_targets(session, "core", today=TODAY)
    assert [w.symbol for w in targets.weights] == ["XIC"]
    assert targets.weights[0].broad_etf  # by symbol, even without asset_class


def test_latest_targets_without_a_snapshot_says_how_to_make_one(session: Session):
    with pytest.raises(ValueError, match="Snapshot: Run Monthly"):
        latest_targets(session, "core", today=TODAY)
