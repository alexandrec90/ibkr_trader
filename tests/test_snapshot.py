"""Forward-shadow persistence, integrity, reporting, and CLI warnings."""

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from ibkr_trader import cli
from ibkr_trader.backtest.engine import RegisteredStrategyConfig
from ibkr_trader.backtest.snapshot import (
    SnapshotRunResult,
    assert_snapshot_date,
    build_snapshot_report,
    run_snapshots,
)
from ibkr_trader.db.models import Base, Instrument, PriceBar, StrategySnapshot
from ibkr_trader.signals.eligibility import EligibilityLimits


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as value:
        yield value


def _instrument(session: Session, symbol: str, *, currency: str = "CAD") -> Instrument:
    row = Instrument(
        symbol=symbol,
        exchange="SMART",
        currency=currency,
        sec_type="STK",
        name=symbol,
        asset_class="ETF",
        leveraged=False,
    )
    session.add(row)
    session.flush()
    return row


def _bar(session: Session, instrument: Instrument, day: date, close: float) -> None:
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


def test_snapshot_upsert_is_idempotent_per_strategy_and_day(session: Session):
    today = date(2026, 7, 10)
    xeqt = _instrument(session, "XEQT")
    for offset in range(260):
        _bar(session, xeqt, today - timedelta(days=259 - offset), 30.0 + offset / 100)
    config = RegisteredStrategyConfig(
        eligibility=EligibilityLimits(min_history_days=1, min_avg_dollar_volume=0)
    )

    first = run_snapshots(
        session, strategies=["equal_weight"], asof=today, today=today, config=config
    )
    second = run_snapshots(
        session, strategies=["equal_weight"], asof=today, today=today, config=config
    )

    assert len(first.snapshots) == len(second.snapshots) == 1
    assert session.scalar(select(func.count()).select_from(StrategySnapshot)) == 1
    assert second.snapshots[0].weights == {str(xeqt.id): 0.25}


def test_historical_backfill_is_refused():
    with pytest.raises(ValueError, match="refusing to backfill"):
        assert_snapshot_date(date(2026, 7, 7), today=date(2026, 7, 10))


def test_report_uses_only_bars_strictly_after_snapshot(session: Session):
    asset = _instrument(session, "ASSET")
    xeqt = _instrument(session, "XEQT")
    snapshot_day = date(2026, 1, 10)
    # The absurd snapshot-day ASSET close must not enter the realized return. The forward
    # return is 100 -> 110 (+10%); XEQT is flat, so excess is +10%.
    for instrument, values in ((asset, [10_000.0, 100.0, 110.0]), (xeqt, [50.0, 100.0, 100.0])):
        for day, close in zip(
            (snapshot_day, snapshot_day + timedelta(days=1), date(2026, 2, 10)),
            values,
            strict=True,
        ):
            _bar(session, instrument, day, close)
    session.add(
        StrategySnapshot(
            strategy="test",
            model_version="1",
            feature_set_version="1",
            ts=datetime.combine(snapshot_day, datetime.min.time(), tzinfo=UTC),
            weights={str(asset.id): 1.0},
            params={"benchmark": "XEQT"},
            created_at=datetime.now(tz=UTC),
        )
    )
    session.flush()

    rows = build_snapshot_report(session, horizon_months=1, asof=date(2026, 2, 10))

    assert len(rows) == 1
    assert rows[0].mean_excess == pytest.approx(0.10)
    assert rows[0].hit_rate == 1.0


def test_empty_db_behavior(session: Session):
    assert build_snapshot_report(session) == []
    with pytest.raises(ValueError, match="no daily price bars"):
        run_snapshots(session, strategies=["equal_weight"])


def test_report_with_only_immature_snapshots_needs_no_benchmark(session: Session):
    session.add(
        StrategySnapshot(
            strategy="test",
            model_version="1",
            feature_set_version="1",
            ts=datetime(2026, 7, 10, tzinfo=UTC),
            weights={},
            params={},
            created_at=datetime.now(tz=UTC),
        )
    )
    session.flush()

    assert build_snapshot_report(session, horizon_months=1, asof=date(2026, 7, 10)) == []


def test_snapshot_run_warns_when_latest_bar_is_stale(monkeypatch):
    @contextmanager
    def fake_session():
        yield object()

    stale = SnapshotRunResult(
        snapshots=[SimpleNamespace(ts=datetime(2026, 7, 10, tzinfo=UTC))],
        skipped={},
        latest_bar_date=date(2026, 6, 30),
        stale_days=10,
    )
    monkeypatch.setattr("ibkr_trader.db.session.get_session", fake_session)
    monkeypatch.setattr("ibkr_trader.backtest.snapshot.run_snapshots", lambda *a, **k: stale)

    result = CliRunner().invoke(cli.app, ["snapshot", "run", "--all"])

    assert result.exit_code == 0
    assert "WARNING" in (result.output + result.stderr)
    assert "run ingestion first" in (result.output + result.stderr)
