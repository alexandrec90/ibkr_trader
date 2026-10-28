"""The paper loop: planning, US-only universe, cadence, windows, and the order hand-off.

No gateway anywhere: the broker is a fake that records the ``OrderRequest``s it is handed,
and the database is in-memory SQLite with a synthetic price history.
"""

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader import paper
from ibkr_trader.backtest.snapshot import upsert_snapshot
from ibkr_trader.db.models import (
    Base,
    Execution,
    Instrument,
    Order,
    OrderSide,
    PriceBar,
    StrategySnapshot,
)
from ibkr_trader.execution import ledger
from ibkr_trader.execution.broker import Position
from ibkr_trader.execution.risk import RiskViolation
from ibkr_trader.signals.eligibility import Candidate

TODAY = date(2026, 10, 27)
#: Tuesday 2026-10-27, New York wall clock -> UTC (EDT, UTC-4).
NY_1100 = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
NY_0945 = datetime(2026, 10, 27, 13, 45, tzinfo=UTC)
NY_1610 = datetime(2026, 10, 27, 20, 10, tzinfo=UTC)
NY_1700 = datetime(2026, 10, 27, 21, 0, tzinfo=UTC)


def _settings(**overrides):
    values = {
        "environment": "paper",
        "paper_strategy": "equal_weight",
        "paper_capital_cad": 100_000.0,
        "paper_max_bar_age_days": 4,
        "paper_trading_enabled": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def session_factory():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return session_factory


def _instrument(session, symbol, *, exchange="SMART", currency="USD", sec_type="STK"):
    row = Instrument(
        symbol=symbol,
        exchange=exchange,
        currency=currency,
        sec_type=sec_type,
        asset_class="ETF" if symbol == "XEQT" else "STK",
        leveraged=False,
    )
    session.add(row)
    session.flush()
    return row


def _bars(session, instrument, *, last_day=TODAY, days=300, start=100.0, step=0.1):
    for offset in range(days):
        day = last_day - timedelta(days=days - 1 - offset)
        close = start + step * offset
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


@pytest.fixture
def market(db):
    """Two US stocks, one TSX ETF (the benchmark), and USDCAD at 1.4."""
    with db() as session:
        ids = {}
        for symbol, exchange, currency in (
            ("AAPL", "SMART", "USD"),
            ("MSFT", "NASDAQ", "USD"),
            ("XEQT", "TSX", "CAD"),
        ):
            row = _instrument(session, symbol, exchange=exchange, currency=currency)
            _bars(session, row, start=100.0 if currency == "USD" else 30.0, step=0.0)
            ids[symbol] = row.id
        fx = _instrument(session, "USDCAD", exchange="IDEALPRO", currency="CAD", sec_type="CASH")
        _bars(session, fx, start=1.4, step=0.0)
    return ids


class FakeBroker:
    def __init__(self, positions=(), *, refuse=(), halted=False):
        self._positions = list(positions)
        self.refuse = set(refuse)
        self._halted = halted
        self.requests = []
        self.connected = None
        self.disconnected = False
        self.synced = 0

    def positions(self):
        return list(self._positions)

    def halted(self):
        return self._halted

    def place_order(self, request):
        self.requests.append(request)
        if request.symbol in self.refuse:
            raise RiskViolation(f"{request.symbol} refused")
        return 100 + len(self.requests)

    def connect(self, *, readonly=True):
        self.connected = readonly

    def disconnect(self):
        self.disconnected = True

    def sync_orders(self):
        self.synced += 1
        return SimpleNamespace(orders_updated=0, executions_added=2, working=0, unmatched=0)


def _order_row(instrument_id, status, *, quantity=1):
    return Order(
        environment="paper",
        instrument_id=instrument_id,
        side=OrderSide.BUY,
        quantity=quantity,
        order_type="MKT",
        status=status,
        ibkr_order_id=1,
        ibkr_perm_id=1,
        placed_at=NY_1100,
        raw={},
    )


def _snapshots(db, name):
    with db() as session:
        return list(
            session.scalars(
                select(StrategySnapshot)
                .where(StrategySnapshot.strategy == name)
                .order_by(StrategySnapshot.ts)
            )
        )


# --- plan_rebalance (pure) ----------------------------------------------------------------


def test_plan_buys_toward_target_rounding_down():
    # 25% of 100,000 CAD at 140 CAD/share = 178.6 -> 178
    assert paper.plan_rebalance({1: 0.25}, {}, {1: 140.0}, 100_000.0, 0.05) == [(1, 178)]


def test_plan_skips_drifts_inside_the_band():
    held = {1: 170.0}  # 23.8% vs 25% target
    assert paper.plan_rebalance({1: 0.25}, held, {1: 140.0}, 100_000.0, 0.05) == []


def test_plan_trims_toward_zero_and_sells_a_dropped_name_in_full():
    held = {1: 300.0, 2: 100.0}  # 42% and 14% of the book
    trades = paper.plan_rebalance({1: 0.25}, held, {1: 140.0, 2: 140.0}, 100_000.0, 0.05)
    # trim 1: (25,000 - 42,000) / 140 = -121.4 -> -121; name 2 left the targets
    assert trades == [(1, -121), (2, -100)]


def test_plan_sends_sells_before_buys():
    held = {5: 300.0}
    trades = paper.plan_rebalance({1: 0.2, 5: 0.0}, held, {1: 100.0, 5: 100.0}, 100_000.0, 0.05)
    assert trades == [(5, -300), (1, 200)]


def test_plan_leaves_an_unpriced_name_alone_and_an_empty_book_untraded():
    assert paper.plan_rebalance({1: 0.5}, {2: 10.0}, {1: 0.0}, 100_000.0, 0.05) == []
    assert paper.plan_rebalance({1: 0.5}, {}, {1: 10.0}, 0.0, 0.05) == []


def test_plan_skips_a_dust_delta():
    # 5.5% target at a 6,000 CAD share price of a 100k book: 0.9 share rounds to nothing
    assert paper.plan_rebalance({1: 0.055}, {}, {1: 6_000.0}, 100_000.0, 0.05) == []


# --- universe, cadence, windows -----------------------------------------------------------


def _candidate(currency, exchange):
    return Candidate(
        instrument_id=1,
        symbol="X",
        currency=currency,
        exchange=exchange,
        last_price=10.0,
        avg_dollar_volume=1e7,
        history_days=500,
    )


@pytest.mark.parametrize(
    ("currency", "exchange", "allowed"),
    [
        ("USD", "SMART", True),
        ("USD", "NASDAQ", True),
        ("usd", "nyse", True),
        ("CAD", "TSX", False),
        ("USD", "TSX", False),  # a US-dollar line on a Canadian marketplace
        ("CAD", "SMART", False),
        ("USD", "NEO", False),
    ],
)
def test_only_us_listings_are_traded(currency, exchange, allowed):
    assert paper.is_us_listing(_candidate(currency, exchange)) is allowed


def test_rebalance_is_due_once_a_calendar_month(db):
    with db() as session:
        assert paper.rebalance_due(session, "equal_weight", TODAY)
        upsert_snapshot(
            session,
            strategy="equal_weight",  # the forward-shadow row is not the paper decision
            asof=TODAY,
            model_version="2",
            weights={},
            params={},
        )
        assert paper.rebalance_due(session, "equal_weight", TODAY)
        upsert_snapshot(
            session,
            strategy="paper:equal_weight",
            asof=date(2026, 10, 1),
            model_version="2",
            weights={},
            params={},
        )
        assert not paper.rebalance_due(session, "equal_weight", TODAY)
        assert paper.rebalance_due(session, "equal_weight", date(2026, 11, 2))
        assert paper.rebalance_due(session, "momentum_lt", TODAY)


@pytest.mark.parametrize(
    ("now", "rebalance", "sync"),
    [
        (NY_0945, False, True),  # open, but the opening auction has not settled
        (NY_1100, True, True),
        (NY_1610, False, True),  # after the bell: fills still arriving
        (NY_1700, False, False),
        (datetime(2026, 10, 31, 15, 0, tzinfo=UTC), False, False),  # Saturday
    ],
)
def test_windows(now, rebalance, sync):
    assert paper.in_rebalance_window(now) is rebalance
    assert paper.in_sync_window(now) is sync


# --- build_plan ---------------------------------------------------------------------------


def test_plan_buys_the_us_names_and_never_the_tsx_benchmark(db, market):
    with db() as session:
        plan = paper.build_plan(session, [], _settings(), TODAY)

    assert set(plan.weights) == {market["AAPL"], market["MSFT"]}
    assert [(t.symbol, t.side, t.quantity) for t in plan.trades] == [
        ("AAPL", "BUY", 178),  # 25% cap of 100,000 CAD at 100 USD x 1.4
        ("MSFT", "BUY", 178),
    ]
    assert plan.trades[0].price == 100.0
    assert plan.trades[0].price_cad == pytest.approx(140.0)
    assert plan.value_cad == plan.cash_cad == 100_000.0
    assert (plan.stale_days, plan.latest_bar_date) == (0, TODAY)
    assert plan.strategy == "equal_weight"


def test_plan_values_held_positions_and_reports_strangers(db, market):
    positions = [
        Position(symbol="AAPL", quantity=210, avg_cost=90.0),  # 29.4k of 129.4k = 22.7%: in band
        Position(symbol="ZZZZ", quantity=5, avg_cost=1.0),
        Position(symbol="XEQT", quantity=0, avg_cost=0.0, currency="CAD"),  # closed: ignored
    ]
    with db() as session:
        plan = paper.build_plan(session, positions, _settings(), TODAY)

    assert plan.value_cad == pytest.approx(100_000.0 + 210 * 140.0)
    assert [t.symbol for t in plan.trades] == ["MSFT"]
    assert plan.unmapped_positions == ["ZZZZ (USD)"]


def test_plan_sizes_from_the_sleeve_not_the_account(db, market):
    with db() as session:
        row = ledger.record_order(session, _order_row(market["AAPL"], "Filled", quantity=100))
        ledger.upsert_execution(
            session,
            Execution(
                order_id=row.id,
                ibkr_exec_id="E1",
                ts=NY_1100,
                quantity=100,
                price=100.0,
                commission=0.0,
                raw={"fx_to_cad": 1.4},
            ),
        )
        plan = paper.build_plan(
            session, [Position(symbol="AAPL", quantity=100, avg_cost=100.0)], _settings(), TODAY
        )

    assert plan.cash_cad == pytest.approx(86_000.0)
    assert plan.value_cad == pytest.approx(100_000.0)


def test_paper_decisions_are_recorded_apart_from_the_forward_shadow():
    assert paper.snapshot_name("momentum_lt") == "paper:momentum_lt"
    assert paper.snapshot_name("momentum_lt") != "momentum_lt"


def test_format_trade_shows_size_cost_and_weights():
    trade = paper.PlannedTrade(
        instrument_id=1,
        symbol="AAPL",
        currency="USD",
        side="BUY",
        quantity=178,
        price=100.0,
        price_cad=140.0,
        target_weight=0.25,
        current_weight=0.0,
    )

    line = paper.format_trade(trade)

    assert line.startswith("BUY     178 AAPL")
    assert "~100.00 USD" in line
    assert "24,920 CAD" in line
    assert "0.0% ->  25.0%" in line


def test_a_decision_loads_the_feature_pipelines_window_not_all_history(db, market, monkeypatch):
    from ibkr_trader.signals import features

    assert paper.DECISION_LOOKBACK_DAYS == features._HISTORY_BUFFER_DAYS
    seen = {}
    real = paper.decision_inputs

    def spy(session, asof, config, **kwargs):
        seen.update(kwargs)
        return real(session, asof, config, **kwargs)

    monkeypatch.setattr(paper, "decision_inputs", spy)
    with db() as session:
        paper.build_plan(session, [], _settings(), TODAY)

    assert seen == {"lookback_days": paper.DECISION_LOOKBACK_DAYS}


def test_a_malformed_series_is_reported_not_fatal(db, market):
    with db() as session:
        junk = _instrument(session, "JUNK")
        _bars(session, junk, start=0.0001, step=0.0)
        broken = session.scalars(
            select(PriceBar).where(PriceBar.instrument_id == junk.id).order_by(PriceBar.ts)
        ).first()
        broken.open = broken.high = broken.low = 0.000001
    broker = FakeBroker()

    result = paper.run_rebalance(broker, _settings(), session_factory=db, now=NY_1100)

    assert list(result.plan.invalid_series) == ["JUNK"]
    assert [r.symbol for r in broker.requests] == ["AAPL", "MSFT"]
    (snapshot,) = _snapshots(db, "paper:equal_weight")
    assert snapshot.params["invalid_series"] == ["JUNK"]


def test_plan_refuses_stale_bars(db, market):
    with db() as session:
        with pytest.raises(paper.PaperTradingBlocked, match="5 days old"):
            paper.build_plan(session, [], _settings(), TODAY + timedelta(days=5))


def test_plan_refuses_an_unknown_strategy(db, market):
    with db() as session:
        with pytest.raises(KeyError, match="unknown allocator"):
            paper.build_plan(session, [], _settings(paper_strategy="nope"), TODAY)


def test_the_configured_default_strategy_plans(db, market):
    with db() as session:
        plan = paper.build_plan(session, [], _settings(paper_strategy="momentum_lt"), TODAY)

    assert plan.strategy == "momentum_lt"  # flat prices: no momentum, so all cash
    assert plan.trades == []


# --- run_rebalance ------------------------------------------------------------------------


def test_rebalance_sends_the_plan_and_records_the_month(db, market):
    broker = FakeBroker()

    result = paper.run_rebalance(broker, _settings(), session_factory=db, now=NY_1100)

    assert result.status == "rebalanced"
    assert result.placed == [101, 102]
    requests = [(r.symbol, r.side, r.quantity, r.currency) for r in broker.requests]
    assert requests == [("AAPL", "BUY", 178, "USD"), ("MSFT", "BUY", 178, "USD")]
    assert broker.requests[0].instrument_id == market["AAPL"]
    assert broker.requests[0].reference_price == 100.0
    (snapshot,) = _snapshots(db, "paper:equal_weight")
    assert snapshot.ts.date() == TODAY
    assert snapshot.params["orders"] == [101, 102]
    assert snapshot.params["universe"] == "us-listings"
    assert set(snapshot.weights) == {str(market["AAPL"]), str(market["MSFT"])}

    again = paper.run_rebalance(broker, _settings(), session_factory=db, now=NY_1100)
    assert again.status == "not-due"
    assert len(broker.requests) == 2


def test_a_refused_order_is_recorded_and_raised(db, market):
    broker = FakeBroker(refuse={"AAPL"})

    with pytest.raises(paper.PaperRebalanceIncomplete, match="AAPL refused"):
        paper.run_rebalance(broker, _settings(), session_factory=db, now=NY_1100)

    assert [r.symbol for r in broker.requests] == ["AAPL", "MSFT"]  # one refusal stops nothing
    (snapshot,) = _snapshots(db, "paper:equal_weight")
    assert snapshot.params["refused"] == {"AAPL": "AAPL refused"}
    assert snapshot.params["orders"] == [102]


def test_rebalance_waits_for_working_orders_even_when_forced(db, market):
    with db() as session:
        ledger.record_order(session, _order_row(market["AAPL"], "Submitted"))
    broker = FakeBroker()

    result = paper.run_rebalance(broker, _settings(), session_factory=db, now=NY_1100, force=True)

    assert result.status == "waiting"
    assert broker.requests == []


def test_force_rebalances_a_finished_month(db, market):
    with db() as session:
        upsert_snapshot(
            session,
            strategy="paper:equal_weight",
            asof=date(2026, 10, 1),
            model_version="2",
            weights={},
            params={},
        )
    broker = FakeBroker()

    result = paper.run_rebalance(broker, _settings(), session_factory=db, now=NY_1100, force=True)

    assert result.status == "rebalanced"
    assert len(broker.requests) == 2


def test_a_halted_rebalance_sends_nothing(db, market):
    broker = FakeBroker(halted=True)

    with pytest.raises(paper.PaperTradingBlocked, match="halted"):
        paper.run_rebalance(broker, _settings(), session_factory=db, now=NY_1100)

    assert broker.requests == []
    assert _snapshots(db, "paper:equal_weight") == []


def test_a_dry_run_plans_and_records_nothing(db, market):
    broker = FakeBroker(halted=True)  # a dry run reads; the halt only stops orders

    result = paper.run_rebalance(broker, _settings(), session_factory=db, now=NY_1100, dry_run=True)

    assert result.status == "dry-run"
    assert len(result.plan.trades) == 2
    assert broker.requests == []
    assert _snapshots(db, "paper:equal_weight") == []


def test_the_trading_day_is_new_york_s(db, market):
    """23:30 New York on Oct 26 is already Oct 27 in UTC; the decision belongs to Oct 26."""
    late = datetime(2026, 10, 27, 3, 30, tzinfo=UTC)
    result = paper.run_rebalance(
        FakeBroker(), _settings(), session_factory=db, now=late, dry_run=True
    )

    assert result.plan.asof == date(2026, 10, 26)


# --- the serve job ------------------------------------------------------------------------


def test_job_is_a_no_op_while_switched_off():
    job = paper.paper_trading_job(_settings(paper_trading_enabled=False))

    assert job() == paper.PAPER_TRADING_OFF


def test_job_connects_to_nothing_outside_market_hours(db):
    factory_calls = []
    job = paper.paper_trading_job(
        _settings(),
        broker_factory=lambda: factory_calls.append(1),
        session_factory=db,
        clock=lambda: NY_1700,
    )

    assert job() == "skipped: outside US market hours"
    assert factory_calls == []


def test_job_only_syncs_outside_the_rebalance_window(db, monkeypatch):
    broker = FakeBroker()
    monkeypatch.setattr(paper, "run_rebalance", lambda *a, **k: pytest.fail("no rebalance"))
    job = paper.paper_trading_job(
        _settings(), broker_factory=lambda: broker, session_factory=db, clock=lambda: NY_1610
    )

    assert job() == {"orders_updated": 0, "executions_added": 2, "working": 0}
    assert broker.connected is False  # a trading session: sync needs orders and fills
    assert broker.synced == 1
    assert broker.disconnected


def test_job_rebalances_inside_the_window(db, market):
    broker = FakeBroker()
    job = paper.paper_trading_job(
        _settings(), broker_factory=lambda: broker, session_factory=db, clock=lambda: NY_1100
    )

    result = job()

    assert result["rebalance"] == "2 order(s) sent, 0 refused"
    assert len(broker.requests) == 2
    assert broker.disconnected


def test_job_disconnects_when_the_rebalance_fails(db, market):
    broker = FakeBroker(refuse={"AAPL"})
    job = paper.paper_trading_job(
        _settings(), broker_factory=lambda: broker, session_factory=db, clock=lambda: NY_1100
    )

    with pytest.raises(paper.PaperRebalanceIncomplete):
        job()

    assert broker.disconnected
