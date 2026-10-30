"""The database side of trading: the ``orders``/``executions`` audit trail and what it implies.

Kept apart from the broker so every rule here is tested against in-memory SQLite, with no
gateway: what was already sent today (the daily risk cap), which orders are still working
(the rebalance waits on them), and how much cash the strategy's sleeve holds.

Notional and FX are stored in each row's ``raw`` JSON rather than new columns: the order's
``notional_cad`` when it is sent, each fill's ``fx_to_cad`` when it is recorded.
"""

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ibkr_trader.db.models import Execution, Instrument, Order, OrderSide, PriceBar

#: IBKR order states after which nothing more happens to an order (ib_async's DoneStates).
DONE_STATES = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})

#: How far back a stored FX bar may be and still convert today's orders.
FX_MAX_AGE_DAYS = 10


def fx_to_cad(session: Session, currency: str, asof: date) -> float | None:
    """CAD per unit of ``currency`` from the newest stored daily bar on or before ``asof``.

    CAD is 1.0. USD reads the ``USDCAD`` CASH series the FX polls keep current. ``None`` when
    there is no bar within ``FX_MAX_AGE_DAYS`` — a stale rate must not size an order.
    """
    if currency.upper() == "CAD":
        return 1.0
    if currency.upper() != "USD":
        return None
    end = datetime.combine(asof, time.min, tzinfo=UTC) + timedelta(days=1)
    row = session.execute(
        select(PriceBar.close, PriceBar.ts)
        .join(Instrument, Instrument.id == PriceBar.instrument_id)
        .where(
            Instrument.symbol == "USDCAD",
            Instrument.sec_type == "CASH",
            PriceBar.bar_size == "1 day",
            PriceBar.ts < end,
            PriceBar.ts >= end - timedelta(days=FX_MAX_AGE_DAYS + 1),
        )
        .order_by(PriceBar.ts.desc())
        .limit(1)
    ).first()
    return float(row.close) if row is not None and row.close > 0 else None


def _utc_midnight(moment: datetime) -> datetime:
    return datetime.combine(moment.astimezone(UTC).date(), time.min, tzinfo=UTC)


def traded_today_cad(session: Session, environment: str, now: datetime) -> float:
    """Sum of ``notional_cad`` over the orders sent since UTC midnight of ``now``."""
    rows = session.scalars(
        select(Order).where(Order.environment == environment, Order.placed_at >= _utc_midnight(now))
    )
    return sum(float((row.raw or {}).get("notional_cad") or 0.0) for row in rows)


def working_orders(session: Session, environment: str) -> list[Order]:
    """Orders sent and not yet in a done state, oldest first."""
    return list(
        session.scalars(
            select(Order)
            .where(Order.environment == environment, Order.status.not_in(DONE_STATES))
            .order_by(Order.id)
        )
    )


def record_order(session: Session, row: Order) -> Order:
    """Persist a transmitted order and return it with its ``id``.

    IBKR reports "no perm id yet" as 0; it is stored as NULL so ``sync_orders`` fills it in.
    """
    if not row.ibkr_perm_id:
        row.ibkr_perm_id = None
    session.add(row)
    session.flush()
    return row


def update_order_status(
    row: Order,
    *,
    status: str,
    filled: float,
    avg_fill_price: float,
    perm_id: int,
) -> bool:
    """Copy a broker-side order status onto its row. Returns whether anything changed."""
    before = (row.status, row.filled_quantity, row.avg_fill_price, row.ibkr_perm_id)
    if status:
        row.status = status
    row.filled_quantity = float(filled)
    row.avg_fill_price = float(avg_fill_price) if avg_fill_price else row.avg_fill_price
    if perm_id:
        row.ibkr_perm_id = perm_id
    return before != (row.status, row.filled_quantity, row.avg_fill_price, row.ibkr_perm_id)


def upsert_execution(session: Session, fill: Execution) -> bool:
    """Insert an (unsaved) fill keyed on IBKR's ``execId``, or refresh the stored one's
    commission. True if inserted.

    The commission report follows its execution, so a fill first recorded without one gets
    it on a later sync.
    """
    row = session.scalar(select(Execution).where(Execution.ibkr_exec_id == fill.ibkr_exec_id))
    if row is None:
        session.add(fill)
        session.flush()
        return True
    if fill.commission is not None and row.commission != fill.commission:
        row.commission = fill.commission
    return False


def sleeve_cash_cad(session: Session, environment: str, capital_cad: float) -> float:
    """The strategy's cash: its starting capital, less what its fills spent, plus what they raised.

    Each fill is converted at the ``fx_to_cad`` recorded with it (1.0 when missing, i.e. CAD).
    Commissions are charged in the same currency as the fill.
    """
    total = float(capital_cad)
    rows = session.execute(
        select(Execution, Order.side)
        .join(Order, Order.id == Execution.order_id)
        .where(Order.environment == environment)
    )
    for execution, side in rows:
        fx = float((execution.raw or {}).get("fx_to_cad") or 1.0)
        gross = execution.quantity * execution.price * fx
        total += -gross if side == OrderSide.BUY else gross
        total -= (execution.commission or 0.0) * fx
    return total
