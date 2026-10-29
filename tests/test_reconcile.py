"""Reconciling the orders/executions ledger with the gateway's Trades and Fills."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ibkr_trader.db.models import Base, Execution, Instrument, Order, OrderSide
from ibkr_trader.execution import reconcile

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as value:
        instrument = Instrument(symbol="AAPL", exchange="SMART", currency="USD", sec_type="STK")
        value.add(instrument)
        value.flush()
        value.info["iid"] = instrument.id
        yield value


def _row(session, *, order_id=41, perm_id=941, status="Submitted", placed_at=NOW, client_id=1):
    row = Order(
        environment="paper",
        instrument_id=session.info["iid"],
        side=OrderSide.BUY,
        quantity=10,
        order_type="MKT",
        status=status,
        ibkr_order_id=order_id,
        ibkr_perm_id=perm_id,
        placed_at=placed_at,
        raw={"client_id": client_id, "currency": "USD"},
    )
    session.add(row)
    session.flush()
    return row


def _trade(order_id=41, perm_id=941, client_id=1, status="Filled", filled=10.0, avg=100.5):
    return SimpleNamespace(
        order=SimpleNamespace(orderId=order_id, permId=perm_id, clientId=client_id),
        orderStatus=SimpleNamespace(status=status, filled=filled, avgFillPrice=avg),
    )


def _fill(exec_id="E1", order_id=41, perm_id=941, client_id=1, commission_exec_id="E1"):
    return SimpleNamespace(
        execution=SimpleNamespace(
            execId=exec_id,
            orderId=order_id,
            permId=perm_id,
            clientId=client_id,
            shares=10,
            price=100.5,
            side="BOT",
            time=NOW,
        ),
        commissionReport=SimpleNamespace(execId=commission_exec_id, commission=1.0, currency="USD"),
        time=NOW,
    )


def test_find_trade_prefers_the_perm_id(session):
    row = _row(session)
    other = _trade(order_id=41, perm_id=1, client_id=1)
    mine = _trade(order_id=99, perm_id=941)

    assert reconcile.find_trade([other, mine], row) is mine


def test_find_trade_falls_back_to_order_and_client_id(session):
    row = _row(session, perm_id=None)

    assert reconcile.find_trade([_trade(perm_id=0)], row) is not None
    assert reconcile.find_trade([_trade(perm_id=0, client_id=2)], row) is None  # another client
    assert reconcile.find_trade([], row) is None


def test_recent_orders_keep_old_working_ones(session):
    fresh = _row(session)
    old_done = _row(session, status="Filled", placed_at=NOW - timedelta(days=30))
    old_working = _row(session, status="Submitted", placed_at=NOW - timedelta(days=30))

    rows = reconcile.recent_orders(session, "paper", NOW - timedelta(days=7))

    assert {r.id for r in rows} == {fresh.id, old_working.id}
    assert old_done.id not in {r.id for r in rows}
    assert reconcile.recent_orders(session, "live", NOW - timedelta(days=7)) == []


def test_update_statuses_counts_changes_and_strangers(session):
    filled = _row(session)
    lost = _row(session, order_id=50, perm_id=950)
    done = _row(session, order_id=60, perm_id=960, status="Cancelled")

    updated, unmatched = reconcile.update_statuses([filled, lost, done], [_trade()])

    assert (updated, unmatched) == (1, 1)
    assert (filled.status, filled.filled_quantity, filled.avg_fill_price) == ("Filled", 10, 100.5)
    assert lost.status == "Submitted"
    assert done.status == "Cancelled"  # done rows are never re-read


def test_record_fills_takes_only_our_orders_with_their_days_rate(session):
    row = _row(session)
    looked_up = []

    def fx(currency, day):
        looked_up.append((currency, day))
        return 1.4

    added = reconcile.record_fills(
        session,
        [row],
        [_fill(), _fill(exec_id="X", order_id=7, perm_id=12345), _fill(commission_exec_id="")],
        fx,
    )

    assert added == 1  # the stranger is skipped; the repeat of E1 is not new
    (execution,) = session.scalars(select(Execution))
    assert (execution.order_id, execution.quantity, execution.price) == (row.id, 10, 100.5)
    assert execution.commission == 1.0
    assert execution.raw == {"side": "BOT", "fx_to_cad": 1.4, "commission_currency": "USD"}
    assert looked_up[0] == ("USD", NOW.date())


def test_sync_orders_is_one_pass_over_both(session):
    _row(session)
    _row(session, order_id=50, perm_id=950)

    result = reconcile.sync_orders(
        session, [_trade()], [_fill()], environment="paper", since=NOW - timedelta(days=7)
    )

    assert result == reconcile.SyncResult(
        orders_updated=1, executions_added=1, working=1, unmatched=1
    )
