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
    DecisionInputs,
    SnapshotRunResult,
    assert_snapshot_date,
    build_snapshot_report,
    decision_inputs,
    load_valid_universe,
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


def test_decision_inputs_use_only_bars_up_to_the_decision_day(session: Session):
    today = date(2026, 7, 10)
    xeqt = _instrument(session, "XEQT")
    for offset in range(260):
        _bar(session, xeqt, today - timedelta(days=257 - offset), 30.0 + offset / 100)
    config = RegisteredStrategyConfig(
        eligibility=EligibilityLimits(min_history_days=1, min_avg_dollar_volume=0)
    )

    inputs = decision_inputs(session, today, config)

    assert isinstance(inputs, DecisionInputs)
    assert [c.symbol for c in inputs.eligible] == ["XEQT"]
    assert inputs.latest_bar_date == today  # the two later bars are not seen
    assert inputs.stale_days == 0
    series = inputs.universe[xeqt.id]
    assert series.dates[-1] == today
    assert xeqt.id in inputs.features


def test_decision_inputs_can_load_only_a_recent_window(session: Session):
    today = date(2026, 7, 10)
    xeqt = _instrument(session, "XEQT")
    gone = _instrument(session, "GONE")
    for offset in range(1_000):
        _bar(session, xeqt, today - timedelta(days=999 - offset), 30.0 + offset / 100)
    for offset in range(100):  # a delisted name whose bars all predate the window
        _bar(session, gone, date(2023, 1, 1) + timedelta(days=offset), 10.0)
    config = RegisteredStrategyConfig(
        eligibility=EligibilityLimits(min_history_days=1, min_avg_dollar_volume=0)
    )

    full = decision_inputs(session, today, config)
    window = decision_inputs(session, today, config, lookback_days=550)

    assert set(full.universe) == {xeqt.id, gone.id}
    assert set(window.universe) == {xeqt.id}
    series = window.universe[xeqt.id]
    assert series.dates[0] == today - timedelta(days=550)
    assert series.dates[-1] == today
    # the window caps history_days, as the feature pipeline's buffer does
    assert window.features[xeqt.id]["history_days"] == 551
    assert full.features[xeqt.id]["history_days"] == 1_000
    # everything a 252-trading-day feature needs is still inside the window
    assert window.features[xeqt.id]["return_12m"] == pytest.approx(
        full.features[xeqt.id]["return_12m"]
    )


def test_a_series_with_a_malformed_bar_is_left_out_and_reported(session: Session):
    """A placeholder row (close above its high) once aborted the whole monthly snapshot."""
    today = date(2026, 7, 10)
    xeqt = _instrument(session, "XEQT")
    bad = _instrument(session, "JUNK")
    for offset in range(260):
        day = today - timedelta(days=259 - offset)
        _bar(session, xeqt, day, 30.0 + offset / 100)
        _bar(session, bad, day, 0.0001)
    broken = session.scalars(
        select(PriceBar).where(PriceBar.instrument_id == bad.id).order_by(PriceBar.ts.desc())
    ).first()
    broken.open = broken.high = broken.low = 0.000001  # close 0.0001 is now above the high
    session.flush()
    config = RegisteredStrategyConfig(
        eligibility=EligibilityLimits(min_history_days=1, min_avg_dollar_volume=0)
    )

    universe, invalid = load_valid_universe(
        session,
        ["XEQT", "JUNK", "NOSUCH"],
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 7, 11, tzinfo=UTC),
    )
    inputs = decision_inputs(session, today, config)
    result = run_snapshots(
        session, strategies=["equal_weight"], asof=today, today=today, config=config
    )

    assert set(universe) == {xeqt.id}  # NOSUCH: no instrument, simply absent
    assert list(invalid) == ["JUNK"]
    assert "close_within_high_low" in invalid["JUNK"]
    assert set(inputs.universe) == {xeqt.id}
    assert list(inputs.invalid) == ["JUNK"]
    assert list(result.invalid) == ["JUNK"]
    assert result.snapshots[0].params["invalid_series"] == ["JUNK"]


def test_decision_inputs_refuse_an_empty_database(session: Session):
    with pytest.raises(ValueError, match="no daily price bars"):
        decision_inputs(session, date(2026, 7, 10), RegisteredStrategyConfig())


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
