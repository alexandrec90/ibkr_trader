"""The paper-trading loop: one registered strategy, traded on the DU paper account.

Each month the loop computes the strategy's target weights exactly as the forward-shadow
snapshot does (``backtest.snapshot.decision_inputs``), restricted to **US listings**, sizes
them against the strategy's sleeve (``PAPER_CAPITAL_CAD`` plus what its fills earned or
spent), and sends the trades the backtest engine would make: the same rebalance band, sells
first, whole shares. Every order goes through ``IbkrBroker.place_order`` and so through
``assert_trading_allowed()``, the paper-account check and ``RiskChecker.check()``.

Why US listings only: IBKR Canada does not let its Canadian-resident clients submit API
orders for products on a Canadian marketplace (CIRO), so a TSX order could never go live and
paper results built on one would not carry over. See docs/operations/paper-trading.md.

The month's decision is recorded as a ``strategy_snapshots`` row named ``paper:<strategy>``,
which is both the forward record (`snapshot report` scores it) and the cadence: a month with
that row is done. Nothing rebalances while an earlier order is still working.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ibkr_trader.backtest.engine import RegisteredStrategyConfig, Series, _fx_to_cad
from ibkr_trader.backtest.snapshot import decision_inputs, upsert_snapshot
from ibkr_trader.db.models import StrategySnapshot
from ibkr_trader.execution import ledger
from ibkr_trader.execution.broker import OrderRequest, Position
from ibkr_trader.execution.risk import US_MARKET_TIMEZONE, RiskViolation, is_us_regular_hours
from ibkr_trader.signals.eligibility import Candidate
from ibkr_trader.signals.portfolio import get_allocator

logger = logging.getLogger(__name__)

#: Prefix of the strategy name the paper loop's monthly decisions are recorded under.
PAPER_PREFIX = "paper:"
#: Canadian marketplaces as ``instruments.exchange`` spells them (TSX is what ingestion
#: stores today; the rest guard listings a later connector may add).
CANADIAN_EXCHANGES = frozenset({"TSX", "TSE", "TSXV", "VENTURE", "CSE", "NEO", "CBOE CANADA"})
#: New York wall-clock window in which a rebalance may start: after the opening auction has
#: settled, early enough that market orders fill in the same session.
REBALANCE_WINDOW = (time(10, 0), time(15, 30))
#: Fills keep arriving for a moment after the bell; the job syncs until this, then stops.
SYNC_UNTIL = time(16, 30)
#: Calendar days of bars a decision loads: the margin ``signals.features`` gives its
#: 252-trading-day windows. Loading all history instead ran a 16 GB desktop out of memory,
#: and the job runs in a 1.5 GB container against a 768 MB database.
DECISION_LOOKBACK_DAYS = 550


class PaperTradingBlocked(RuntimeError):
    """The loop refused to rebalance (stale bars, halted, nothing to trade from)."""


class PaperRebalanceIncomplete(RuntimeError):
    """The rebalance was recorded, but at least one of its orders was refused."""


def is_us_listing(candidate: Candidate) -> bool:
    """A USD listing outside the Canadian marketplaces."""
    return (
        candidate.currency.upper() == "USD" and candidate.exchange.upper() not in CANADIAN_EXCHANGES
    )


def snapshot_name(strategy: str) -> str:
    return f"{PAPER_PREFIX}{strategy}"


def plan_rebalance(
    targets: dict[int, float],
    holdings: dict[int, float],
    prices_cad: dict[int, float],
    value_cad: float,
    band: float,
) -> list[tuple[int, int]]:
    """Whole-share trades toward ``targets``: ``[(instrument_id, delta_shares)]``, sells first.

    The backtest engine's rule (``engine._execute``): a name trades only when its weight is
    more than ``band`` away from target. A name with no price is left alone. A name leaving
    the targets is sold in full; every other delta rounds toward zero, so a buy never spends
    more than its target and a trim never sells more than its drift.
    """
    if value_cad <= 0:
        return []
    deltas: list[tuple[int, int]] = []
    for iid in sorted(set(holdings) | set(targets)):
        price = prices_cad.get(iid)
        if price is None or not price > 0:
            continue
        held = holdings.get(iid, 0.0)
        current_w = held * price / value_cad
        target_w = targets.get(iid, 0.0)
        if abs(target_w - current_w) <= band:
            continue
        if target_w == 0:
            delta = -int(held)
        else:
            delta = int((target_w * value_cad - held * price) / price)  # toward zero
        if delta:
            deltas.append((iid, delta))
    deltas.sort(key=lambda kv: (kv[1] > 0, kv[0]))  # sells (negative) first
    return deltas


@dataclass(frozen=True)
class PlannedTrade:
    instrument_id: int
    symbol: str
    currency: str
    side: str
    quantity: int
    price: float  # last stored close, listing currency
    price_cad: float
    target_weight: float
    current_weight: float


@dataclass(frozen=True)
class PaperPlan:
    strategy: str
    model_version: str
    asof: date
    latest_bar_date: date
    stale_days: int
    cash_cad: float
    value_cad: float
    weights: dict[int, float]
    trades: list[PlannedTrade]
    unmapped_positions: list[str] = field(default_factory=list)
    #: Symbols left out of the decision because their bars failed validation, with why.
    invalid_series: dict[str, str] = field(default_factory=dict)


def _active_series_by_listing(universe: dict[int, Series], today: date) -> dict[tuple, Series]:
    """``(SYMBOL, CURRENCY)`` → the live series; a reused ticker maps to its newest bars."""
    out: dict[tuple, Series] = {}
    for series in universe.values():
        if series.idx_asof(today) < 0 or series.has_ended(today):
            continue
        key = (series.symbol.upper(), series.currency.upper())
        if key not in out or series.dates[-1] > out[key].dates[-1]:
            out[key] = series
    return out


def build_plan(
    session: Session,
    positions: list[Position],
    settings: Any,
    today: date,
    *,
    config: RegisteredStrategyConfig | None = None,
) -> PaperPlan:
    """Decide today's paper trades. Pure DB reads plus ``positions``; sends nothing."""
    config = config or RegisteredStrategyConfig()
    inputs = decision_inputs(session, today, config, lookback_days=DECISION_LOOKBACK_DAYS)
    if inputs.stale_days > settings.paper_max_bar_age_days:
        raise PaperTradingBlocked(
            f"newest daily bar is {inputs.stale_days} days old (limit "
            f"{settings.paper_max_bar_age_days}): is the app container's ingestion running?"
        )
    eligible = [c for c in inputs.eligible if is_us_listing(c)]
    allocator = get_allocator(settings.paper_strategy)
    allocator.asof(today)
    weights = allocator.allocate(eligible, inputs.features)

    listings = _active_series_by_listing(inputs.universe, today)
    holdings: dict[int, float] = {}
    unmapped: list[str] = []
    for position in positions:
        if not position.quantity:
            continue
        series = listings.get((position.symbol.upper(), position.currency.upper()))
        if series is None:
            unmapped.append(f"{position.symbol} ({position.currency})")
            continue
        holdings[series.instrument_id] = holdings.get(series.instrument_id, 0.0) + float(
            position.quantity
        )

    prices: dict[int, float] = {}
    prices_cad: dict[int, float] = {}
    for iid in set(holdings) | set(weights):
        series = inputs.universe[iid]
        i = series.idx_asof(today)
        if i < 0:
            continue
        prices[iid] = series.closes[i]
        prices_cad[iid] = series.closes[i] * _fx_to_cad(series.currency, inputs.fx, today)

    cash = ledger.sleeve_cash_cad(session, settings.environment, settings.paper_capital_cad)
    value = cash + sum(holdings[iid] * prices_cad.get(iid, 0.0) for iid in holdings)
    deltas = plan_rebalance(weights, holdings, prices_cad, value, config.rebalance_band)
    trades = []
    for iid, delta in deltas:
        series = inputs.universe[iid]
        trades.append(
            PlannedTrade(
                instrument_id=iid,
                symbol=series.symbol,
                currency=series.currency,
                side="BUY" if delta > 0 else "SELL",
                quantity=abs(delta),
                price=prices[iid],
                price_cad=prices_cad[iid],
                target_weight=weights.get(iid, 0.0),
                current_weight=holdings.get(iid, 0.0) * prices_cad[iid] / value if value else 0.0,
            )
        )
    return PaperPlan(
        strategy=settings.paper_strategy,
        model_version=allocator.version,
        asof=today,
        latest_bar_date=inputs.latest_bar_date,
        stale_days=inputs.stale_days,
        cash_cad=cash,
        value_cad=value,
        weights=weights,
        trades=trades,
        unmapped_positions=unmapped,
        invalid_series=inputs.invalid,
    )


def rebalance_due(session: Session, strategy: str, today: date) -> bool:
    """True when this calendar month has no ``paper:<strategy>`` decision yet."""
    latest = session.scalar(
        select(StrategySnapshot.ts)
        .where(StrategySnapshot.strategy == snapshot_name(strategy))
        .order_by(StrategySnapshot.ts.desc())
        .limit(1)
    )
    return latest is None or (latest.year, latest.month) < (today.year, today.month)


def in_rebalance_window(now: datetime) -> bool:
    local = now.astimezone(US_MARKET_TIMEZONE)
    start, end = REBALANCE_WINDOW
    return local.weekday() < 5 and start <= local.time() < end


def in_sync_window(now: datetime) -> bool:
    local = now.astimezone(US_MARKET_TIMEZONE)
    return is_us_regular_hours(now) or (
        local.weekday() < 5 and time(16, 0) <= local.time() < SYNC_UNTIL
    )


@dataclass(frozen=True)
class RebalanceResult:
    status: str  # "waiting" | "not-due" | "dry-run" | "rebalanced"
    message: str
    plan: PaperPlan | None = None
    placed: list[int] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)


def run_rebalance(
    broker: Any,
    settings: Any,
    *,
    session_factory: Callable[[], AbstractContextManager[Session]],
    now: datetime | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> RebalanceResult:
    """Plan this month's trades and, unless ``dry_run``, send them through ``broker``.

    Skips (no error) while an earlier order is still working or when the month is done
    (``force`` ignores the month, never the working orders). Raises
    ``PaperRebalanceIncomplete`` after recording the month when any order was refused, so
    `health` goes red and the month is not retried into the same refusal every 15 minutes.
    """
    now = now or datetime.now(UTC)
    today = now.astimezone(US_MARKET_TIMEZONE).date()
    strategy = settings.paper_strategy
    with session_factory() as session:
        working = ledger.working_orders(session, settings.environment)
        if working:
            return RebalanceResult(
                "waiting", f"{len(working)} earlier order(s) still working; run `paper sync`"
            )
        if not force and not rebalance_due(session, strategy, today):
            return RebalanceResult("not-due", f"{strategy} already rebalanced this month")
    if not dry_run and broker.halted():
        raise PaperTradingBlocked("trading is halted (TRADING_HALTED or the halt file)")

    positions = broker.positions()
    with session_factory() as session:
        plan = build_plan(session, positions, settings, today)
    if dry_run:
        return RebalanceResult("dry-run", f"{len(plan.trades)} trade(s) planned", plan)

    from ibkr_trader.execution.ibkr_broker import OrderRejected

    placed: list[int] = []
    refused: dict[str, str] = {}
    for trade in plan.trades:
        request = OrderRequest(
            symbol=trade.symbol,
            side=trade.side,
            quantity=trade.quantity,
            currency=trade.currency,
            instrument_id=trade.instrument_id,
            reference_price=trade.price,
        )
        try:
            placed.append(broker.place_order(request))
        except (RiskViolation, OrderRejected) as exc:
            logger.warning(
                "paper order refused: %s %s %s: %s", trade.side, trade.quantity, trade.symbol, exc
            )
            refused[trade.symbol] = str(exc)

    with session_factory() as session:
        _record_month(session, plan, settings, placed, refused)
    message = f"{len(placed)} order(s) sent, {len(refused)} refused"
    if refused:
        raise PaperRebalanceIncomplete(f"{message}: {refused}")
    return RebalanceResult("rebalanced", message, plan, placed, refused)


def _record_month(
    session: Session, plan: PaperPlan, settings: Any, placed: list[int], refused: dict[str, str]
) -> None:
    """The month's decision as a ``paper:<strategy>`` snapshot: its forward record and cadence."""
    upsert_snapshot(
        session,
        strategy=snapshot_name(plan.strategy),
        asof=plan.asof,
        model_version=plan.model_version,
        weights=plan.weights,
        params={
            "capital_cad": settings.paper_capital_cad,
            "value_cad": round(plan.value_cad, 2),
            "cash_cad": round(plan.cash_cad, 2),
            "bar_asof": plan.latest_bar_date.isoformat(),
            "stale_days": plan.stale_days,
            "universe": "us-listings",
            "orders": placed,
            "refused": refused,
            "unmapped_positions": plan.unmapped_positions,
            "invalid_series": sorted(plan.invalid_series),
        },
    )


#: The paper job's recorded result when it is switched off, so `health` shows why.
PAPER_TRADING_OFF = "skipped: PAPER_TRADING_ENABLED is false"


def paper_trading_job(
    settings: Any,
    *,
    broker_factory: Callable[[], Any] | None = None,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Callable[[], object]:
    """The ``paper`` serve job: sync fills, and rebalance once a month inside the window.

    Outside US market hours (plus a short post-close sync) it does nothing and connects to
    nothing, so the gateway's nightly restart never reads as a failed job.
    """
    if not settings.paper_trading_enabled:
        return lambda: PAPER_TRADING_OFF

    def job() -> object:
        now = clock()
        if not in_sync_window(now):
            return "skipped: outside US market hours"
        if broker_factory is None:
            from ibkr_trader.execution.ibkr_broker import IbkrBroker

            broker = IbkrBroker()
        else:
            broker = broker_factory()
        if session_factory is None:
            from ibkr_trader.db.session import get_session

            sessions: Callable[[], AbstractContextManager[Session]] = get_session
        else:
            sessions = session_factory
        broker.connect(readonly=False)
        try:
            sync = broker.sync_orders()
            summary: dict[str, object] = {
                "orders_updated": sync.orders_updated,
                "executions_added": sync.executions_added,
                "working": sync.working,
            }
            if sync.unmatched:
                summary["unmatched"] = sync.unmatched
            if not in_rebalance_window(now):
                return summary
            result = run_rebalance(broker, settings, session_factory=sessions, now=now)
            summary["rebalance"] = result.message
            return summary
        finally:
            broker.disconnect()

    return job


def format_trade(trade: PlannedTrade) -> str:
    notional = trade.quantity * trade.price_cad
    return (
        f"{trade.side:<4} {trade.quantity:>6} {trade.symbol:<8} @ ~{trade.price:,.2f} "
        f"{trade.currency}  ~{notional:>10,.0f} CAD  weight {trade.current_weight:6.1%} -> "
        f"{trade.target_weight:6.1%}"
    )
