"""IbkrBroker against a fake ``ib_async.IB`` and in-memory SQLite — never a live gateway.

The order path is pinned end to end: the paper gate and account resolution run first, then
``RiskChecker.check()``, and only then the whatIf preflight and the transmit; every
transmitted order lands in ``orders``.
"""

import math
from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ib_async.ib import StartupFetch, StartupFetchALL
from ib_async.wrapper import RequestError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.config import Settings
from ibkr_trader.db.models import Base, Execution, Instrument, Order, OrderSide, PriceBar
from ibkr_trader.execution import ibkr_broker, ledger, reconcile
from ibkr_trader.execution.broker import OrderRequest, Position
from ibkr_trader.execution.risk import RiskViolation
from ibkr_trader.gateway_watch import GatewayNotLoggedIn, GatewayNotRunning, GatewayWrongLogin

PAPER = "DU1234567"
#: Tuesday 2026-10-27 11:00 New York: inside regular trading hours.
OPEN = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


def _paper_settings(**overrides):
    values = {"environment": "paper", "ibkr_paper_account": PAPER, "_env_file": None}
    values.update(overrides)
    return Settings(**values)


def _broker(monkeypatch, settings):
    monkeypatch.setattr(ibkr_broker, "get_settings", lambda: settings)
    broker = ibkr_broker.IbkrBroker()
    broker.risk = Mock()
    return broker


class FakeIB:
    """Just enough of ``ib_async.IB`` for the broker: records what it was asked."""

    RequestTimeout = 0.0

    def __init__(self, accounts=(PAPER,), connect_error=None):
        self.accounts = list(accounts)
        self.connect_error = connect_error
        self.connect_args = None
        self.disconnects = 0
        self.positions_for = None
        self.summary_for = None
        self.market_data_type = None
        self.cancelled = []
        self.sleeps = 0
        self.qualify_result = None  # None => qualify succeeds
        self.ticker = SimpleNamespace(
            bid=math.nan, ask=math.nan, last=101.5, close=100.0, marketDataType=3
        )
        self.held = [("AAPL", "USD", 265598, 10, 150.25)]
        self.calls = []  # the order-side calls, in order
        self.what_if_error = None
        self.ack_status = "Submitted"
        self.ack_log = []
        self.next_order_id = 41
        self.trade_list = []
        self.fill_list = []
        self.cancelled_orders = []

    def connect(self, host, port, **kwargs):
        self.connect_args = {"host": host, "port": port, **kwargs}
        if self.connect_error is not None:
            raise self.connect_error

    def disconnect(self):
        self.disconnects += 1

    def managedAccounts(self):
        return list(self.accounts)

    def reqCurrentTime(self):
        return datetime(2026, 10, 26, 14, 0, tzinfo=UTC)

    def positions(self, account=""):
        self.positions_for = account
        return [
            SimpleNamespace(
                account=account,
                contract=SimpleNamespace(symbol=symbol, currency=currency, conId=con_id),
                position=quantity,
                avgCost=avg_cost,
            )
            for symbol, currency, con_id, quantity, avg_cost in self.held
        ]

    def accountSummary(self, account=""):
        self.summary_for = account
        return [
            SimpleNamespace(tag="AccountType", value="INDIVIDUAL", currency=""),
            SimpleNamespace(tag="NetLiquidation", value="1000000", currency="CAD"),
            SimpleNamespace(tag="CashBalance", value="5", currency="CAD"),
            SimpleNamespace(tag="CashBalance", value="7", currency="USD"),
        ]

    def qualifyContracts(self, *contracts):
        if self.qualify_result is not None:
            return self.qualify_result
        for contract in contracts:
            contract.conId = 265598 if contract.symbol == "AAPL" else 1
        return list(contracts)

    def reqMarketDataType(self, value):
        self.market_data_type = value

    def reqMktData(self, contract):
        self.requested = contract
        return self.ticker

    def cancelMktData(self, contract):
        self.cancelled.append(contract)

    def sleep(self, seconds):
        self.sleeps += 1

    def whatIfOrder(self, contract, order):
        self.calls.append(("whatIf", contract.symbol, order.action, order.totalQuantity))
        if self.what_if_error is not None:
            raise self.what_if_error
        return SimpleNamespace(
            status="PreSubmitted",
            initMarginChange="0",
            equityWithLoanChange="-1.0",
            commission=1.0,
            commissionCurrency="USD",
            warningText="",
        )

    def placeOrder(self, contract, order):
        self.calls.append(("place", contract.symbol, order.action, order.totalQuantity))
        self.placed_order = order
        order.orderId = self.next_order_id
        order.clientId = 1
        order.permId = 900 + self.next_order_id
        trade = SimpleNamespace(
            contract=contract,
            order=order,
            orderStatus=SimpleNamespace(
                status=self.ack_status, filled=0.0, remaining=0.0, avgFillPrice=0.0
            ),
            log=[SimpleNamespace(message=m) for m in self.ack_log],
        )
        self.trade_list.append(trade)
        return trade

    def trades(self):
        return list(self.trade_list)

    def fills(self):
        return list(self.fill_list)

    def cancelOrder(self, order):
        self.cancelled_orders.append(order.orderId)


def _connected(monkeypatch, fake=None, *, readonly=True, **overrides):
    fake = fake or FakeIB()
    monkeypatch.setattr(ibkr_broker, "get_settings", lambda: _paper_settings(**overrides))
    broker = ibkr_broker.IbkrBroker(ib_factory=lambda: fake)
    broker.connect(readonly=readonly)
    return broker, fake


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

    with session_factory() as session:
        aapl = Instrument(symbol="AAPL", exchange="SMART", currency="USD", sec_type="STK")
        fx = Instrument(symbol="USDCAD", exchange="IDEALPRO", currency="CAD", sec_type="CASH")
        session.add_all([aapl, fx])
        session.flush()
        session.add(
            PriceBar(
                instrument_id=fx.id,
                ts=datetime(2026, 10, 26, tzinfo=UTC),
                bar_size="1 day",
                source="yahoo",
                what_to_show="MIDPOINT",
                open=1.4,
                high=1.4,
                low=1.4,
                close=1.4,
                volume=0,
            )
        )
    session_factory.aapl_id = aapl.id
    return session_factory


def _trading(monkeypatch, db, fake=None, tmp_path=None, **overrides):
    """A broker connected for trading, on the fake, the test DB and an open-market clock."""
    fake = fake or FakeIB()
    if tmp_path is not None:
        overrides.setdefault("trading_halt_file", str(tmp_path / "TRADING_HALT"))
    monkeypatch.setattr(ibkr_broker, "get_settings", lambda: _paper_settings(**overrides))
    broker = ibkr_broker.IbkrBroker(ib_factory=lambda: fake, session_factory=db, clock=lambda: OPEN)
    broker.connect(readonly=False)
    return broker, fake


def _buy(db, quantity=10, **overrides):
    values = {
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": quantity,
        "instrument_id": db.aapl_id,
        "reference_price": 100.0,
    }
    values.update(overrides)
    return OrderRequest(**values)


def _orders(db):
    with db() as session:
        return list(session.scalars(select(Order).order_by(Order.id)))


# --- order path: the gates before anything is sent ----------------------------------------


def test_place_order_never_routes_to_configured_funded_account(monkeypatch):
    settings = Settings(
        environment="paper",
        ibkr_paper_account="U-TFSA",
        ibkr_tfsa_account="U-TFSA",
        _env_file=None,
    )
    broker = _broker(monkeypatch, settings)
    request = OrderRequest(symbol="AAPL", side="BUY", quantity=1, account="tfsa")

    with pytest.raises(RuntimeError, match="starting with DU"):
        broker.place_order(request)

    broker.risk.check.assert_not_called()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"environment": "dev"}, "disabled in ENVIRONMENT=dev"),
        ({"environment": "live"}, "LIVE_TRADING_ACKNOWLEDGED"),
        ({"environment": "live", "live_trading_acknowledged": True}, "live-account routing"),
    ],
)
def test_place_order_refuses_every_environment_but_paper(monkeypatch, overrides, message):
    broker = _broker(monkeypatch, _paper_settings(**overrides))

    with pytest.raises(RuntimeError, match=message):
        broker.place_order(OrderRequest(symbol="AAPL", side="BUY", quantity=1))

    broker.risk.check.assert_not_called()


def test_place_order_needs_a_connection(monkeypatch):
    broker = _broker(monkeypatch, _paper_settings())

    with pytest.raises(RuntimeError, match="not connected"):
        broker.place_order(OrderRequest(symbol="AAPL", side="BUY", quantity=1))


def test_place_order_refuses_a_read_only_session(monkeypatch, db):
    broker, fake = _connected(monkeypatch, readonly=True)

    with pytest.raises(RuntimeError, match="read-only"):
        broker.place_order(_buy(db))

    assert fake.calls == []


def test_place_order_needs_an_instrument_for_the_audit_trail(monkeypatch, db, tmp_path):
    broker, fake = _trading(monkeypatch, db, tmp_path=tmp_path)

    with pytest.raises(ValueError, match="instrument_id"):
        broker.place_order(_buy(db, instrument_id=None))

    assert fake.calls == []


# --- order path: risk before transmit -----------------------------------------------------


def test_place_order_checks_risk_then_preflights_then_transmits(monkeypatch, db, tmp_path):
    fake = FakeIB()
    order_of_events = []
    broker, fake = _trading(monkeypatch, db, fake=fake, tmp_path=tmp_path)
    real_check = broker.risk.check

    def check(request, context):
        order_of_events.append(("risk", list(fake.calls)))
        real_check(request, context)

    broker.risk.check = check

    local_id = broker.place_order(_buy(db))

    assert order_of_events == [("risk", [])]  # nothing preflighted or sent before the check
    assert [call[0] for call in fake.calls] == ["whatIf", "place"]
    assert fake.placed_order.account == PAPER
    assert fake.placed_order.orderType == "MKT"
    assert fake.placed_order.tif == "DAY"
    (row,) = _orders(db)
    assert row.id == local_id
    assert row.environment == "paper"
    assert row.instrument_id == db.aapl_id
    assert row.side.value == "BUY"
    assert row.quantity == 10
    assert row.status == "Submitted"
    assert (row.ibkr_order_id, row.ibkr_perm_id) == (41, 941)
    assert row.raw["quote_price"] == 101.5  # no ask on the fake: last
    assert row.raw["fx_to_cad"] == 1.4
    assert row.raw["notional_cad"] == pytest.approx(10 * 101.5 * 1.4)
    assert row.raw["preflight"]["commission"] == 1.0
    assert row.raw["client_id"] == 1


def test_the_risk_context_is_built_from_the_world(monkeypatch, db, tmp_path):
    broker, fake = _trading(monkeypatch, db, tmp_path=tmp_path)
    fake.ticker = SimpleNamespace(bid=99.0, ask=101.0, last=100.0, close=98.0, marketDataType=3)
    with db() as session:
        ledger.record_order(
            session,
            Order(
                environment="paper",
                instrument_id=db.aapl_id,
                side=OrderSide.BUY,
                quantity=1,
                order_type="MKT",
                status="Filled",
                ibkr_order_id=1,
                ibkr_perm_id=1,
                placed_at=OPEN,
                raw={"notional_cad": 5_000.0},
            ),
        )
    seen = {}
    broker.risk.check = lambda request, context: seen.update(context=context)

    broker.place_order(_buy(db))
    broker.place_order(_buy(db, side="SELL", quantity=5))

    context = seen["context"]
    assert context.price == 99.0  # a sell pays the bid
    assert context.fx_to_cad == 1.4
    assert context.position_quantity == 10  # matched on conId
    assert context.traded_today_cad == pytest.approx(5_000.0 + 10 * 101.0 * 1.4)
    assert context.now == OPEN
    assert context.halted is False


def test_a_risk_violation_sends_and_records_nothing(monkeypatch, db, tmp_path):
    broker, fake = _trading(monkeypatch, db, tmp_path=tmp_path)

    with pytest.raises(RiskViolation, match="per-order cap"):
        broker.place_order(_buy(db, quantity=500))  # 500 x 101.5 x 1.4 = 71k CAD

    assert fake.calls == []
    assert _orders(db) == []


def test_the_halt_file_stops_every_order(monkeypatch, db, tmp_path):
    broker, fake = _trading(monkeypatch, db, tmp_path=tmp_path)
    (tmp_path / "TRADING_HALT").write_text("stop", encoding="utf-8")

    with pytest.raises(RiskViolation, match="halted"):
        broker.place_order(_buy(db))

    assert broker.halted()
    assert fake.calls == []


def test_the_halt_flag_stops_every_order(monkeypatch, db, tmp_path):
    broker, fake = _trading(monkeypatch, db, tmp_path=tmp_path, trading_halted=True)

    with pytest.raises(RiskViolation, match="halted"):
        broker.place_order(_buy(db))

    assert fake.calls == []


def test_a_wrong_contract_is_caught_by_the_reference_price(monkeypatch, db, tmp_path):
    broker, fake = _trading(monkeypatch, db, tmp_path=tmp_path)

    with pytest.raises(RiskViolation, match="wrong contract"):
        broker.place_order(_buy(db, reference_price=20.0))

    assert fake.calls == []


def test_a_missing_fx_rate_refuses_the_order(monkeypatch, db, tmp_path):
    late = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)  # the stored USDCAD bar is too old
    monkeypatch.setattr(
        ibkr_broker, "get_settings", lambda: _paper_settings(trading_halt_file=str(tmp_path / "H"))
    )
    fake = FakeIB()
    broker = ibkr_broker.IbkrBroker(ib_factory=lambda: fake, session_factory=db, clock=lambda: late)
    broker.connect(readonly=False)

    with pytest.raises(RiskViolation, match="CAD rate"):
        broker.place_order(_buy(db))

    assert fake.calls == []


def test_an_unresolvable_symbol_sends_nothing(monkeypatch, db, tmp_path):
    fake = FakeIB()
    fake.qualify_result = [None]
    broker, fake = _trading(monkeypatch, db, fake=fake, tmp_path=tmp_path)

    with pytest.raises(RuntimeError, match="could not resolve"):
        broker.place_order(_buy(db))

    assert fake.calls == []


# --- order path: what IBKR says -----------------------------------------------------------


@pytest.mark.parametrize(
    "error", [RequestError(7, 201, "rejected: no permissions"), TimeoutError()]
)
def test_a_refused_preflight_transmits_nothing(monkeypatch, db, tmp_path, error):
    fake = FakeIB()
    fake.what_if_error = error
    broker, fake = _trading(monkeypatch, db, fake=fake, tmp_path=tmp_path)

    with pytest.raises(ibkr_broker.OrderRejected, match="preflight"):
        broker.place_order(_buy(db))

    assert [call[0] for call in fake.calls] == ["whatIf"]
    assert _orders(db) == []


def test_an_order_ibkr_kills_at_once_is_recorded_and_reported(monkeypatch, db, tmp_path):
    fake = FakeIB()
    fake.ack_status = "Inactive"
    fake.ack_log = ["", "Order rejected - reason: API is read-only"]
    broker, fake = _trading(monkeypatch, db, fake=fake, tmp_path=tmp_path)

    with pytest.raises(ibkr_broker.OrderRejected, match="read-only"):
        broker.place_order(_buy(db))

    (row,) = _orders(db)
    assert row.status == "Inactive"
    assert row.raw["messages"] == ["Order rejected - reason: API is read-only"]


def test_a_limit_order_carries_its_price(monkeypatch, db, tmp_path):
    broker, fake = _trading(monkeypatch, db, tmp_path=tmp_path)

    broker.place_order(_buy(db, order_type="LMT", limit_price=100.0))

    assert fake.placed_order.orderType == "LMT"
    assert fake.placed_order.lmtPrice == 100.0
    assert _orders(db)[0].raw["notional_cad"] == pytest.approx(10 * 100.0 * 1.4)


def test_the_ack_wait_is_bounded(monkeypatch, db, tmp_path):
    fake = FakeIB()
    fake.ack_status = "PendingSubmit"
    monkeypatch.setattr(ibkr_broker, "ACK_TIMEOUT_SECONDS", 0.0)
    broker, fake = _trading(monkeypatch, db, fake=fake, tmp_path=tmp_path)

    broker.place_order(_buy(db))

    assert _orders(db)[0].status == "PendingSubmit"


# --- sync and cancel ----------------------------------------------------------------------


def _fill(order, *, exec_id="E1", shares=10, price=100.5, commission_exec_id="E1", client_id=1):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol="AAPL"),
        execution=SimpleNamespace(
            execId=exec_id,
            permId=order.permId,
            orderId=order.orderId,
            clientId=client_id,
            shares=shares,
            price=price,
            side="BOT",
            time=OPEN,
        ),
        commissionReport=SimpleNamespace(execId=commission_exec_id, commission=1.0, currency="USD"),
        time=OPEN,
    )


def test_sync_copies_statuses_and_fills(monkeypatch, db, tmp_path):
    broker, fake = _trading(monkeypatch, db, tmp_path=tmp_path)
    local_id = broker.place_order(_buy(db))
    trade = fake.trade_list[0]
    trade.orderStatus = SimpleNamespace(status="Filled", filled=10.0, avgFillPrice=100.5)
    fake.fill_list = [
        _fill(trade.order),
        _fill(
            SimpleNamespace(permId=12345, orderId=7), exec_id="X", commission_exec_id=""
        ),  # someone else's order
    ]

    result = broker.sync_orders()

    assert result == reconcile.SyncResult(
        orders_updated=1, executions_added=1, working=0, unmatched=0
    )
    with db() as session:
        row = session.get(Order, local_id)
        assert (row.status, row.filled_quantity, row.avg_fill_price) == ("Filled", 10.0, 100.5)
        (execution,) = session.scalars(select(Execution))
        assert execution.order_id == local_id
        assert (execution.quantity, execution.price, execution.commission) == (10, 100.5, 1.0)
        assert execution.raw["fx_to_cad"] == 1.4
    assert broker.sync_orders().executions_added == 0  # idempotent


def test_sync_records_a_fill_before_its_commission_report(monkeypatch, db, tmp_path):
    broker, fake = _trading(monkeypatch, db, tmp_path=tmp_path)
    broker.place_order(_buy(db))
    order = fake.trade_list[0].order
    fake.fill_list = [_fill(order, commission_exec_id="")]

    broker.sync_orders()
    with db() as session:
        assert session.scalar(select(Execution.commission)) is None

    fake.fill_list = [_fill(order)]
    broker.sync_orders()
    with db() as session:
        assert session.scalar(select(Execution.commission)) == 1.0


def test_sync_counts_orders_the_gateway_forgot(monkeypatch, db, tmp_path):
    broker, fake = _trading(monkeypatch, db, tmp_path=tmp_path)
    broker.place_order(_buy(db))
    fake.trade_list = []

    result = broker.sync_orders()

    assert (result.working, result.unmatched) == (1, 1)


def test_sync_matches_by_order_id_before_a_perm_id_arrives(monkeypatch, db, tmp_path):
    broker, fake = _trading(monkeypatch, db, tmp_path=tmp_path)
    local_id = broker.place_order(_buy(db))
    with db() as session:
        session.get(Order, local_id).ibkr_perm_id = None
    trade = fake.trade_list[0]
    trade.orderStatus = SimpleNamespace(status="Filled", filled=10.0, avgFillPrice=100.5)

    assert broker.sync_orders().orders_updated == 1
    with db() as session:
        assert session.get(Order, local_id).ibkr_perm_id == 941


def test_cancel_asks_ibkr_and_marks_the_row(monkeypatch, db, tmp_path):
    broker, fake = _trading(monkeypatch, db, tmp_path=tmp_path)
    local_id = broker.place_order(_buy(db))

    broker.cancel_order(local_id)

    assert fake.cancelled_orders == [41]
    with db() as session:
        assert session.get(Order, local_id).status == "PendingCancel"


def test_cancel_refuses_unknown_and_finished_orders(monkeypatch, db, tmp_path):
    broker, fake = _trading(monkeypatch, db, tmp_path=tmp_path)
    local_id = broker.place_order(_buy(db))

    with pytest.raises(ValueError, match="no order 999"):
        broker.cancel_order(999)
    fake.trade_list = []
    with pytest.raises(RuntimeError, match="does not know"):
        broker.cancel_order(local_id)
    with db() as session:
        session.get(Order, local_id).status = "Filled"
    with pytest.raises(RuntimeError, match="already Filled"):
        broker.cancel_order(local_id)
    assert fake.cancelled_orders == []


# --- connect ------------------------------------------------------------------------------


def test_connect_binds_the_paper_account_read_only(monkeypatch):
    broker, fake = _connected(monkeypatch, ibkr_host="gw", ibkr_port=4004, ibkr_client_id=7)

    assert broker.account_id == PAPER
    assert fake.connect_args["host"] == "gw"
    assert fake.connect_args["port"] == 4004
    assert fake.connect_args["clientId"] == 7
    assert fake.connect_args["readonly"] is True
    assert fake.connect_args["account"] == PAPER
    # read-only skips the order/execution startup fetch a Read-Only API gateway won't serve
    assert fake.connect_args["fetchFields"] == StartupFetch(0)
    assert fake.disconnects == 0
    # every later blocking request is bounded, never ib_async's default of "forever"
    assert fake.RequestTimeout == ibkr_broker.REQUEST_TIMEOUT_SECONDS


def test_connect_for_trading_fetches_everything(monkeypatch):
    broker, fake = _connected(monkeypatch, readonly=False)

    assert fake.connect_args["readonly"] is False
    assert fake.connect_args["fetchFields"] == StartupFetchALL


@pytest.mark.parametrize(
    "overrides",
    [
        {"environment": "dev"},
        {"environment": "live"},
        {"environment": "live", "live_trading_acknowledged": True},
    ],
)
def test_connect_refuses_every_environment_but_paper(monkeypatch, overrides):
    factory = Mock()
    monkeypatch.setattr(ibkr_broker, "get_settings", lambda: _paper_settings(**overrides))
    broker = ibkr_broker.IbkrBroker(ib_factory=factory)

    with pytest.raises(RuntimeError, match="only connects in ENVIRONMENT=paper"):
        broker.connect()

    factory.assert_not_called()


@pytest.mark.parametrize(
    ("account", "message"),
    [("", "not configured"), ("U7654321", "starting with DU")],
)
def test_connect_refuses_a_bad_paper_account_before_dialing(monkeypatch, account, message):
    factory = Mock()
    monkeypatch.setattr(
        ibkr_broker, "get_settings", lambda: _paper_settings(ibkr_paper_account=account)
    )
    broker = ibkr_broker.IbkrBroker(ib_factory=factory)

    with pytest.raises(RuntimeError, match=message):
        broker.connect()

    factory.assert_not_called()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConnectionRefusedError(), GatewayNotRunning),
        (TimeoutError(), GatewayNotLoggedIn),
        (ConnectionError("Socket connection broken while connecting"), GatewayNotLoggedIn),
    ],
)
def test_connect_classifies_handshake_failures(monkeypatch, error, expected):
    fake = FakeIB(connect_error=error)
    monkeypatch.setattr(ibkr_broker, "get_settings", lambda: _paper_settings())
    broker = ibkr_broker.IbkrBroker(ib_factory=lambda: fake)

    with pytest.raises(expected):
        broker.connect()

    assert broker.account_id == ""


@pytest.mark.parametrize(
    "accounts",
    [
        [],  # answered, but no accounts
        [PAPER, "U7654321"],  # a funded account on the same login
        ["DU7654321"],  # a paper login, but not the configured one
    ],
)
def test_connect_refuses_and_disconnects_a_wrong_login(monkeypatch, accounts):
    fake = FakeIB(accounts=accounts)
    monkeypatch.setattr(ibkr_broker, "get_settings", lambda: _paper_settings())
    broker = ibkr_broker.IbkrBroker(ib_factory=lambda: fake)

    with pytest.raises(GatewayWrongLogin) as excinfo:
        broker.connect()

    assert fake.disconnects == 1
    assert PAPER not in str(excinfo.value)
    with pytest.raises(RuntimeError, match="not connected"):
        broker.positions()


def test_disconnect_closes_once_and_forgets_the_session(monkeypatch):
    broker, fake = _connected(monkeypatch)

    broker.disconnect()
    broker.disconnect()

    assert fake.disconnects == 1
    with pytest.raises(RuntimeError, match="not connected"):
        broker.account_summary()


# --- reads --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    ["positions", "account_summary", "server_time", "managed_accounts", "sync_orders"],
)
def test_reads_require_a_connection(monkeypatch, call):
    broker = _broker(monkeypatch, _paper_settings())

    with pytest.raises(RuntimeError, match="not connected"):
        getattr(broker, call)()


def test_positions_are_the_paper_accounts_only(monkeypatch):
    broker, fake = _connected(monkeypatch)

    assert broker.positions() == [
        Position(symbol="AAPL", quantity=10.0, avg_cost=150.25, currency="USD")
    ]
    assert fake.positions_for == PAPER


def test_account_summary_keys_by_tag_and_currency(monkeypatch):
    broker, fake = _connected(monkeypatch)

    assert broker.account_summary() == {
        "AccountType": "INDIVIDUAL",
        "NetLiquidation:CAD": "1000000",
        "CashBalance:CAD": "5",
        "CashBalance:USD": "7",
    }
    assert fake.summary_for == PAPER


def test_server_time_and_managed_accounts(monkeypatch):
    broker, _ = _connected(monkeypatch)

    assert broker.server_time() == datetime(2026, 10, 26, 14, 0, tzinfo=UTC)
    assert broker.managed_accounts() == [PAPER]


# --- delayed quote ------------------------------------------------------------------------


def test_delayed_quote_requests_delayed_data_and_cancels(monkeypatch):
    broker, fake = _connected(monkeypatch)

    quote = broker.delayed_quote("SPY")

    assert fake.market_data_type == ibkr_broker.DELAYED_MARKET_DATA == 3
    assert fake.requested.symbol == "SPY"
    assert fake.requested.exchange == "SMART"
    assert fake.requested.currency == "USD"
    assert fake.cancelled == [fake.requested]
    assert quote.has_price()
    assert (quote.last, quote.close, quote.market_data_type) == (101.5, 100.0, 3)
    assert math.isnan(quote.bid)


def test_delayed_quote_gives_up_after_the_timeout_and_still_cancels(monkeypatch):
    fake = FakeIB()
    fake.ticker = SimpleNamespace(
        bid=-1.0, ask=-1.0, last=math.nan, close=math.nan, marketDataType=3
    )
    broker, _ = _connected(monkeypatch, fake=fake)

    quote = broker.delayed_quote("XIU", "CAD", timeout=0)

    assert not quote.has_price()
    assert math.isnan(quote.price_for("BUY"))
    assert fake.requested.currency == "CAD"
    assert len(fake.cancelled) == 1


def test_delayed_quote_waits_for_a_tick(monkeypatch):
    fake = FakeIB()
    fake.ticker = SimpleNamespace(
        bid=math.nan, ask=math.nan, last=math.nan, close=math.nan, marketDataType=3
    )

    def tick(seconds):
        fake.sleeps += 1
        fake.ticker.last = 42.0

    fake.sleep = tick
    broker, _ = _connected(monkeypatch, fake=fake)

    quote = broker.delayed_quote("SPY", timeout=60)

    assert quote.last == 42.0
    assert fake.sleeps == 1


@pytest.mark.parametrize("result", [[], [None]])
def test_delayed_quote_refuses_an_unresolvable_symbol(monkeypatch, result):
    fake = FakeIB()
    fake.qualify_result = result
    broker, _ = _connected(monkeypatch, fake=fake)

    with pytest.raises(RuntimeError, match="could not resolve NOPE"):
        broker.delayed_quote("NOPE")

    assert fake.market_data_type is None
    assert fake.cancelled == []


@pytest.mark.parametrize(
    ("side", "bid", "ask", "last", "close", "expected"),
    [
        ("BUY", 99.0, 101.0, 100.0, 98.0, 101.0),
        ("SELL", 99.0, 101.0, 100.0, 98.0, 99.0),
        ("BUY", math.nan, -1.0, 100.0, 98.0, 100.0),
        ("SELL", math.nan, math.nan, math.nan, 98.0, 98.0),
    ],
)
def test_quote_price_for_a_side(side, bid, ask, last, close, expected):
    quote = ibkr_broker.Quote("AAPL", "USD", bid, ask, last, close, 3)

    assert quote.price_for(side) == expected


def test_preflight_summary_drops_unset_commission():
    state = SimpleNamespace(commission=1.7976931348623157e308, warningText="careful")

    summary = ibkr_broker._preflight_summary(state)

    assert summary["commission"] is None
    assert summary["warning"] == "careful"


def test_the_order_count_helper_sees_a_clean_db(db):
    with db() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        assert ledger.fx_to_cad(session, "USD", date(2026, 10, 27)) == 1.4
