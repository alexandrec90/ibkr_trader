"""Reconcile the ``orders``/``executions`` ledger with what the gateway reports.

``orderStatus`` callbacks are not guaranteed for every transition (docs/reference/ibkr/
04-orders.md), so nothing trusts the status an order was recorded with: each sync re-reads
every working order's ``Trade`` and every recent fill, matched on IBKR's ``permId`` (stable
across sessions) or, before one arrives, on ``(orderId, clientId)``.

Works on plain objects shaped like ib_async's ``Trade`` and ``Fill``, so it is tested without
a gateway.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ibkr_trader.db.models import Execution, Order
from ibkr_trader.execution import ledger


@dataclass(frozen=True)
class SyncResult:
    orders_updated: int
    executions_added: int
    working: int  # orders still not in a done state after the sync
    unmatched: int  # working orders the gateway does not know about


def find_trade(trades: Iterable[Any], row: Order) -> Any:
    """The gateway ``Trade`` for an ``orders`` row: by perm id, else by (order id, client id)."""
    trades = list(trades)
    if row.ibkr_perm_id:
        for trade in trades:
            if trade.order.permId == row.ibkr_perm_id:
                return trade
    client_id = (row.raw or {}).get("client_id")
    for trade in trades:
        if (
            row.ibkr_order_id
            and trade.order.orderId == row.ibkr_order_id
            and trade.order.clientId == client_id
        ):
            return trade
    return None


def recent_orders(session: Session, environment: str, since: datetime) -> list[Order]:
    """Orders placed since ``since``, plus any older one still working."""
    return list(
        session.scalars(
            select(Order).where(
                Order.environment == environment,
                (Order.placed_at >= since) | Order.status.not_in(ledger.DONE_STATES),
            )
        )
    )


def update_statuses(rows: list[Order], trades: list[Any]) -> tuple[int, int]:
    """Copy each working row's gateway status. Returns (rows changed, rows the gateway lacks)."""
    updated = unmatched = 0
    for row in rows:
        if row.status in ledger.DONE_STATES:
            continue
        trade = find_trade(trades, row)
        if trade is None:
            unmatched += 1
            continue
        status = trade.orderStatus
        if ledger.update_order_status(
            row,
            status=status.status,
            filled=status.filled,
            avg_fill_price=status.avgFillPrice,
            perm_id=trade.order.permId,
        ):
            updated += 1
    return updated, unmatched


def record_fills(
    session: Session,
    rows: list[Order],
    fills: list[Any],
    fx_lookup: Callable[[str, date], float | None],
) -> int:
    """Upsert every fill that belongs to one of ``rows``; returns how many were new.

    A fill of an order this app did not place (manual, another client) is skipped. Each fill
    keeps the FX rate of its day, which the sleeve's cash is later computed from.
    """
    by_perm = {row.ibkr_perm_id: row for row in rows if row.ibkr_perm_id}
    by_order_id = {
        (row.ibkr_order_id, (row.raw or {}).get("client_id")): row
        for row in rows
        if row.ibkr_order_id
    }
    added = 0
    for fill in fills:
        execution = fill.execution
        owner = by_perm.get(execution.permId) or by_order_id.get(
            (execution.orderId, execution.clientId)
        )
        if owner is None:
            continue
        report = fill.commissionReport
        when = fill.time or execution.time
        currency = (owner.raw or {}).get("currency", "USD")
        record = Execution(
            order_id=owner.id,
            ibkr_exec_id=execution.execId,
            ts=when,
            quantity=float(execution.shares),
            price=float(execution.price),
            commission=report.commission if report.execId else None,
            raw={
                "side": execution.side,
                "fx_to_cad": fx_lookup(currency, when.date()),
                "commission_currency": report.currency,
            },
        )
        if ledger.upsert_execution(session, record):
            added += 1
    return added


def sync_orders(
    session: Session,
    trades: list[Any],
    fills: list[Any],
    *,
    environment: str,
    since: datetime,
) -> SyncResult:
    """One reconciliation pass over the orders placed since ``since`` (and any still working)."""
    rows = recent_orders(session, environment, since)
    updated, unmatched = update_statuses(rows, trades)
    added = record_fills(
        session, rows, fills, lambda currency, day: ledger.fx_to_cad(session, currency, day)
    )
    working = sum(row.status not in ledger.DONE_STATES for row in rows)
    return SyncResult(updated, added, working, unmatched)
