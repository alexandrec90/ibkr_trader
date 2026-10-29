"""The orders/executions ledger: daily notional, working orders, fills, sleeve cash, FX."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ibkr_trader.db.models import Base, Execution, Instrument, Order, OrderSide, PriceBar
from ibkr_trader.execution import ledger

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as value:
        yield value


def _instrument(session: Session, symbol="AAPL", *, sec_type="STK", currency="USD") -> int:
    row = Instrument(symbol=symbol, exchange="SMART", currency=currency, sec_type=sec_type)
    session.add(row)
    session.flush()
    return row.id


def _fx_bar(session: Session, iid: int, day: date, close: float) -> None:
    session.add(
        PriceBar(
            instrument_id=iid,
            ts=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            bar_size="1 day",
            source="yahoo",
            what_to_show="MIDPOINT",
            open=close,
            high=close,
            low=close,
            close=close,
            volume=0,
        )
    )
    session.flush()


def _order(session: Session, iid: int, *, side="BUY", status="Submitted", placed_at=NOW, **raw):
    return ledger.record_order(
        session,
        Order(
            environment="paper",
            instrument_id=iid,
            side=OrderSide(side),
            quantity=10,
            order_type="MKT",
            limit_price=None,
            status=status,
            ibkr_order_id=1,
            ibkr_perm_id=0,
            placed_at=placed_at,
            raw=raw,
        ),
    )


def _fill(order: Order, exec_id: str, quantity: float, price: float, commission, **raw):
    return Execution(
        order_id=order.id,
        ibkr_exec_id=exec_id,
        ts=NOW,
        quantity=quantity,
        price=price,
        commission=commission,
        raw=raw,
    )


def test_cad_needs_no_rate_and_unknown_currencies_get_none(session):
    assert ledger.fx_to_cad(session, "CAD", NOW.date()) == 1.0
    assert ledger.fx_to_cad(session, "EUR", NOW.date()) is None


def test_usd_rate_is_the_newest_bar_on_or_before_the_day(session):
    fx = _instrument(session, "USDCAD", sec_type="CASH", currency="CAD")
    _fx_bar(session, fx, date(2026, 10, 23), 1.38)
    _fx_bar(session, fx, date(2026, 10, 26), 1.40)
    _fx_bar(session, fx, date(2026, 10, 28), 1.45)  # after the day asked for

    assert ledger.fx_to_cad(session, "USD", date(2026, 10, 27)) == 1.40
    assert ledger.fx_to_cad(session, "usd", date(2026, 10, 24)) == 1.38


def test_a_stale_usd_rate_is_no_rate(session):
    fx = _instrument(session, "USDCAD", sec_type="CASH", currency="CAD")
    _fx_bar(session, fx, date(2026, 10, 1), 1.38)

    assert ledger.fx_to_cad(session, "USD", date(2026, 10, 27)) is None
    assert ledger.fx_to_cad(session, "USD", date(2026, 10, 5)) == 1.38


def test_record_order_stores_a_missing_perm_id_as_null(session):
    row = _order(session, _instrument(session))

    assert row.id is not None
    assert row.ibkr_perm_id is None


def test_traded_today_sums_only_todays_orders(session):
    iid = _instrument(session)
    _order(session, iid, notional_cad=10_000.0)
    _order(session, iid, notional_cad=2_500.5)
    _order(session, iid, placed_at=NOW - timedelta(days=1), notional_cad=99_000.0)
    _order(session, iid)  # no notional recorded: counts as zero

    assert ledger.traded_today_cad(session, "paper", NOW) == pytest.approx(12_500.5)
    assert ledger.traded_today_cad(session, "live", NOW) == 0.0


def test_working_orders_are_the_ones_not_done(session):
    iid = _instrument(session)
    working = _order(session, iid, status="Submitted")
    pending = _order(session, iid, status="PreSubmitted")
    for status in sorted(ledger.DONE_STATES):
        _order(session, iid, status=status)

    assert [row.id for row in ledger.working_orders(session, "paper")] == [working.id, pending.id]


def test_update_order_status_reports_changes(session):
    row = _order(session, _instrument(session))

    assert ledger.update_order_status(
        row, status="Filled", filled=10, avg_fill_price=101.5, perm_id=555
    )
    assert (row.status, row.filled_quantity, row.avg_fill_price, row.ibkr_perm_id) == (
        "Filled",
        10.0,
        101.5,
        555,
    )
    assert not ledger.update_order_status(
        row, status="Filled", filled=10, avg_fill_price=101.5, perm_id=555
    )
    # no average yet / no perm id yet: keep what is known
    assert not ledger.update_order_status(row, status="", filled=10, avg_fill_price=0.0, perm_id=0)


def test_execution_upsert_is_keyed_on_exec_id_and_fills_in_the_commission(session):
    row = _order(session, _instrument(session))

    assert ledger.upsert_execution(session, _fill(row, "E1", 10, 100.0, None))
    assert not ledger.upsert_execution(session, _fill(row, "E1", 10, 100.0, 1.0))
    assert not ledger.upsert_execution(session, _fill(row, "E1", 10, 100.0, None))  # keeps 1.0
    session.flush()

    assert session.scalar(select(func.count()).select_from(Execution)) == 1
    assert session.scalar(select(Execution.commission)) == 1.0


def test_sleeve_cash_follows_the_fills(session):
    iid = _instrument(session)
    buy = _order(session, iid, side="BUY")
    sell = _order(session, iid, side="SELL")
    ledger.upsert_execution(session, _fill(buy, "B1", 10, 100.0, 1.0, fx_to_cad=1.4))
    ledger.upsert_execution(session, _fill(sell, "S1", 4, 110.0, 1.0, fx_to_cad=1.5))

    # 100,000 - 10*100*1.4 - 1*1.4 + 4*110*1.5 - 1*1.5
    assert ledger.sleeve_cash_cad(session, "paper", 100_000.0) == pytest.approx(99_257.1)
    assert ledger.sleeve_cash_cad(session, "live", 100_000.0) == 100_000.0


def test_sleeve_cash_treats_a_fill_without_a_rate_as_cad(session):
    row = _order(session, _instrument(session, "XEQT", currency="CAD"))
    ledger.upsert_execution(session, _fill(row, "C1", 10, 30.0, None))

    assert ledger.sleeve_cash_cad(session, "paper", 1_000.0) == pytest.approx(700.0)
