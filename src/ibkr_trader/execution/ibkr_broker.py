"""IBKR implementation of Broker using ib_async (docs: https://ib-api-reloaded.github.io/ib_async/).

Order lifecycle reminders:
- orderStatus callbacks are NOT guaranteed for every transition; reconcile via execDetails
  and periodic reqOpenOrders/reqPositions (``sync_orders``, see execution/reconcile.py).
- use whatIf=True for a margin/commission preflight before transmitting.

A session is only ever opened in ENVIRONMENT=paper, and only when the gateway is logged into
the configured ``DU`` paper account and nothing else. An order is transmitted only after
``assert_trading_allowed()``, the paper-account resolution, ``RiskChecker.check()`` and a
whatIf preflight have all passed, and every transmitted order is recorded in ``orders``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ibkr_trader.config import get_settings
from ibkr_trader.execution import ledger, reconcile
from ibkr_trader.execution.broker import Broker, OrderRequest, Position
from ibkr_trader.execution.risk import RiskChecker, RiskContext, RiskLimits
from ibkr_trader.gateway_watch import (
    _NOT_RUNNING_ERRORS,
    GatewayNotLoggedIn,
    GatewayNotRunning,
    GatewayWrongLogin,
    check_accounts,
)

#: How long ib_async waits for the API handshake and for each startup sync request. A
#: logged-out gateway behind the image's socat forward accepts TCP and never answers.
CONNECT_TIMEOUT_SECONDS = 15.0
#: Bound on every later blocking request (a whatIf preflight, a contract lookup). ib_async's
#: default is 0, i.e. wait forever, which would hang a scheduler job on a wedged gateway.
REQUEST_TIMEOUT_SECONDS = 30.0
#: How long :meth:`IbkrBroker.delayed_quote` waits for a first tick before giving up.
QUOTE_TIMEOUT_SECONDS = 10.0
#: How long ``place_order`` waits for the gateway to acknowledge a transmitted order.
ACK_TIMEOUT_SECONDS = 5.0
#: ``reqMarketDataType`` value for delayed data, which needs no market-data subscription
#: (docs/reference/ibkr/03-market-data-and-historical.md).
DELAYED_MARKET_DATA = 3
#: Orders whose fills ``sync_orders`` still looks for; IBKR reports recent executions only.
SYNC_LOOKBACK_DAYS = 7
#: ib_async's placeholder for "no value" in an OrderState double field.
_UNSET = 1e300


def _new_ib() -> Any:
    from ib_async import IB

    return IB()


class OrderRejected(RuntimeError):
    """IBKR refused the order: at the whatIf preflight, or right after it was transmitted."""


@dataclass(frozen=True)
class Quote:
    """One market-data snapshot. Prices IBKR did not send are ``nan``."""

    symbol: str
    currency: str
    bid: float
    ask: float
    last: float
    close: float
    market_data_type: int  # 1 live, 2 frozen, 3 delayed, 4 delayed-frozen

    def has_price(self) -> bool:
        return any(_usable(p) for p in (self.bid, self.ask, self.last, self.close))

    def price_for(self, side: str) -> float:
        """What a market order would likely pay: the far side of the book, else last/close."""
        far = self.ask if side == "BUY" else self.bid
        for price in (far, self.last, self.close):
            if _usable(price):
                return price
        return math.nan


def _usable(price: float) -> bool:
    return math.isfinite(price) and price > 0


def _default_session_factory() -> AbstractContextManager[Session]:
    from ibkr_trader.db.session import get_session

    return get_session()


class IbkrBroker(Broker):
    def __init__(
        self,
        ib_factory: Callable[[], Any] = _new_ib,
        *,
        session_factory: Callable[[], AbstractContextManager[Session]] = (_default_session_factory),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.settings = get_settings()
        self.risk = RiskChecker(RiskLimits.from_settings(self.settings))
        self._ib_factory = ib_factory
        self._session_factory = session_factory
        self._clock = clock
        self._ib: Any = None  # ib_async.IB instance once connected
        self._readonly = True
        self.account_id = ""  # the DU paper account this session is bound to

    def connect(self, *, readonly: bool = True) -> None:
        """Open an API session and refuse it unless it is on the configured paper account.

        ``readonly`` skips ib_async's startup fetch of orders and executions, which a gateway
        in Read-Only API mode does not serve, and makes ``place_order`` refuse. Trading needs
        ``readonly=False`` and a gateway with READ_ONLY_API=no.
        Raises a ``GatewayDown`` subclass when the gateway is stopped, logged out, or logged
        into the wrong account, and ``RuntimeError`` when this project's config forbids it.
        """
        settings = self.settings
        if settings.environment != "paper":
            raise RuntimeError(
                f"IbkrBroker only connects in ENVIRONMENT=paper (it is {settings.environment})."
            )
        # Validates IBKR_PAPER_ACCOUNT (set, starts with DU, agrees with the legacy field).
        account_id = settings.ibkr_execution_account(settings.default_account)

        from ib_async.ib import StartupFetch, StartupFetchALL

        ib = self._ib_factory()
        address = f"{settings.ibkr_host}:{settings.ibkr_port}"
        try:
            ib.connect(
                settings.ibkr_host,
                settings.ibkr_port,
                clientId=settings.ibkr_client_id,
                timeout=CONNECT_TIMEOUT_SECONDS,
                readonly=readonly,
                account=account_id,
                fetchFields=StartupFetch(0) if readonly else StartupFetchALL,
            )
        except _NOT_RUNNING_ERRORS as exc:
            raise GatewayNotRunning(
                f"nothing listening at {address}: {type(exc).__name__}"
            ) from exc
        except OSError as exc:  # TimeoutError and ConnectionError are both OSError
            raise GatewayNotLoggedIn(
                f"no API handshake with {address}: {type(exc).__name__}"
            ) from exc

        try:
            accounts = list(ib.managedAccounts())
            check_accounts(accounts, settings.environment)
            if account_id not in accounts:
                # Names no account ID: the configured one may be a typo of a real one.
                raise GatewayWrongLogin(
                    "gateway is logged in, but IBKR_PAPER_ACCOUNT is not one of its accounts"
                )
        except BaseException:
            ib.disconnect()
            raise
        ib.RequestTimeout = REQUEST_TIMEOUT_SECONDS
        self._ib = ib
        self._readonly = readonly
        self.account_id = account_id

    def disconnect(self) -> None:
        if self._ib is not None:
            self._ib.disconnect()
            self._ib = None

    def _require_ib(self) -> Any:
        if self._ib is None:
            raise RuntimeError("IbkrBroker is not connected; call connect() first.")
        return self._ib

    def managed_accounts(self) -> list[str]:
        return list(self._require_ib().managedAccounts())

    def server_time(self) -> datetime:
        return self._require_ib().reqCurrentTime()

    def halted(self) -> bool:
        """The emergency stop: ``TRADING_HALTED`` or the halt file's existence."""
        settings = self.settings
        return bool(settings.trading_halted) or Path(settings.trading_halt_file).exists()

    # --- orders ---------------------------------------------------------------------------

    def place_order(self, request: OrderRequest) -> int:
        """Gate, risk-check, preflight, transmit and record one order. Returns ``orders.id``.

        Raises ``RiskViolation`` (nothing sent), ``OrderRejected`` (IBKR refused it: nothing
        sent at preflight, or a recorded order IBKR cancelled at once), ``RuntimeError`` for a
        session or config that may not trade.
        """
        self.settings.assert_trading_allowed()  # paper/live gate — do not remove
        account_id = self.settings.ibkr_execution_account(
            request.account or self.settings.default_account
        )
        self._require_trading_session(account_id, request)
        contract = _qualify(self._require_ib(), request.symbol, request.currency)
        context = self._risk_context(request, contract)
        self.risk.check(request, context)  # every order path — do not remove

        order = self._build_order(request, account_id)
        preflight = self._preflight(contract, order, request.symbol)
        trade = self._require_ib().placeOrder(contract, order)
        self._await_ack(trade)
        return self._record(request, contract, trade, context, preflight)

    def _require_trading_session(self, account_id: str, request: OrderRequest) -> None:
        self._require_ib()
        if self._readonly:
            raise RuntimeError("connected read-only; connect(readonly=False) to place orders")
        if account_id != self.account_id:
            raise RuntimeError("the order's account is not the one this session is bound to")
        if request.instrument_id is None:
            raise ValueError("OrderRequest.instrument_id is required: every order is recorded")

    def _risk_context(self, request: OrderRequest, contract: Any) -> RiskContext:
        """The world as the risk check must see it: a fresh quote, holdings, today, FX, halt."""
        ib = self._require_ib()
        quote = _stream_quote(ib, contract, request.symbol, request.currency)
        held = _held_quantity(
            ib.positions(self.account_id), contract, request.symbol, request.currency
        )
        now = self._clock()
        with self._session_factory() as session:
            fx = ledger.fx_to_cad(session, request.currency, now.date())
            traded = ledger.traded_today_cad(session, self.settings.environment, now)
        return RiskContext(
            price=quote.price_for(request.side),
            fx_to_cad=fx if fx is not None else math.nan,
            position_quantity=held,
            traded_today_cad=traded,
            now=now,
            halted=self.halted(),
        )

    def _preflight(self, contract: Any, order: Any, symbol: str) -> Any:
        from ib_async.wrapper import RequestError

        try:
            return self._require_ib().whatIfOrder(contract, order)
        except (RequestError, TimeoutError) as exc:
            raise OrderRejected(f"IBKR preflight refused {symbol}: {exc}") from exc

    def _record(
        self, request: OrderRequest, contract: Any, trade: Any, context: RiskContext, preflight: Any
    ) -> int:
        """Write the ``orders`` row; raise ``OrderRejected`` if IBKR killed the order at once."""
        from ibkr_trader.db.models import Order, OrderSide

        price = request.limit_price if request.order_type == "LMT" else context.price
        status = trade.orderStatus.status or "PendingSubmit"
        messages = [entry.message for entry in trade.log if getattr(entry, "message", "")]
        row = Order(
            environment=self.settings.environment,
            instrument_id=request.instrument_id,
            side=OrderSide(request.side),
            quantity=request.quantity,
            order_type=request.order_type,
            limit_price=request.limit_price,
            status=status,
            ibkr_order_id=trade.order.orderId or None,
            ibkr_perm_id=trade.order.permId,
            placed_at=context.now,
            raw={
                "symbol": request.symbol,
                "currency": request.currency,
                "con_id": getattr(contract, "conId", 0),
                "client_id": self.settings.ibkr_client_id,
                "quote_price": context.price,
                "reference_price": request.reference_price,
                "fx_to_cad": context.fx_to_cad,
                "notional_cad": request.quantity * float(price or 0.0) * context.fx_to_cad,
                "preflight": _preflight_summary(preflight),
                "messages": messages,
            },
        )
        with self._session_factory() as session:
            local_id = ledger.record_order(session, row).id
        if status in ledger.DONE_STATES and status != "Filled":
            reason = messages[-1] if messages else status
            raise OrderRejected(f"IBKR {status} order {local_id} ({request.symbol}): {reason}")
        return local_id

    def cancel_order(self, local_order_id: int) -> None:
        """Ask IBKR to cancel a working order; ``sync_orders`` records the outcome."""
        from ibkr_trader.db.models import Order

        ib = self._require_ib()
        with self._session_factory() as session:
            row = session.get(Order, local_order_id)
            if row is None:
                raise ValueError(f"no order {local_order_id}")
            if row.status in ledger.DONE_STATES:
                raise RuntimeError(f"order {local_order_id} is already {row.status}")
            trade = reconcile.find_trade(ib.trades(), row)
            if trade is None:
                raise RuntimeError(f"the gateway does not know order {local_order_id}")
            ib.cancelOrder(trade.order)
            row.status = "PendingCancel"

    def sync_orders(self) -> reconcile.SyncResult:
        """Copy order statuses and fills from the gateway onto ``orders``/``executions``.

        Needs a ``readonly=False`` session: only then does the startup fetch load open and
        completed orders and today's executions into ``ib.trades()``/``ib.fills()``.
        """
        ib = self._require_ib()
        since = self._clock() - timedelta(days=SYNC_LOOKBACK_DAYS)
        with self._session_factory() as session:
            return reconcile.sync_orders(
                session,
                list(ib.trades()),
                list(ib.fills()),
                environment=self.settings.environment,
                since=since,
            )

    def _build_order(self, request: OrderRequest, account_id: str) -> Any:
        from ib_async import LimitOrder, MarketOrder

        common = {"account": account_id, "tif": "DAY", "orderRef": "ibkr_trader"}
        if request.order_type == "LMT":
            # RiskChecker has already refused a limit order without a positive price.
            return LimitOrder(request.side, request.quantity, request.limit_price or 0.0, **common)
        return MarketOrder(request.side, request.quantity, **common)

    def _await_ack(self, trade: Any) -> None:
        ib = self._require_ib()
        deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
        while trade.orderStatus.status in ("", "PendingSubmit") or not trade.order.permId:
            if time.monotonic() >= deadline:
                return
            ib.sleep(0.2)

    # --- reads ----------------------------------------------------------------------------

    def positions(self) -> list[Position]:
        """Positions on the paper account only, never another account the login manages."""
        return [
            Position(
                symbol=p.contract.symbol,
                quantity=float(p.position),
                avg_cost=float(p.avgCost),
                currency=getattr(p.contract, "currency", "") or "USD",
            )
            for p in self._require_ib().positions(self.account_id)
        ]

    def account_summary(self) -> dict[str, str]:
        """The paper account's summary as ``{"Tag:CUR": value}``, or ``{"Tag": value}``.

        Keyed by currency too because ib_async also requests ``$LEDGER:ALL``, which sends
        the same tag once per currency (``CashBalance:CAD``, ``CashBalance:USD``, ...).
        """
        return {
            f"{v.tag}:{v.currency}" if v.currency else v.tag: v.value
            for v in self._require_ib().accountSummary(self.account_id)
        }

    def delayed_quote(
        self, symbol: str, currency: str = "USD", *, timeout: float = QUOTE_TIMEOUT_SECONDS
    ) -> Quote:
        """Stream delayed data for one stock until a price arrives or ``timeout`` passes.

        Raises ``RuntimeError`` when IBKR cannot resolve the symbol to exactly one stock.
        A quote that timed out is returned anyway; check :meth:`Quote.has_price`.
        """
        ib = self._require_ib()
        contract = _qualify(ib, symbol, currency)
        return _stream_quote(ib, contract, symbol, currency, timeout=timeout)


def _qualify(ib: Any, symbol: str, currency: str) -> Any:
    """Resolve a SMART-routed stock to exactly one IBKR contract, or raise ``RuntimeError``."""
    from ib_async import Stock

    contract = Stock(symbol, "SMART", currency)
    qualified = ib.qualifyContracts(contract)
    if not qualified or qualified[0] is None:
        raise RuntimeError(f"IBKR could not resolve {symbol} ({currency}) to one stock.")
    return qualified[0]


def _stream_quote(
    ib: Any, contract: Any, symbol: str, currency: str, *, timeout: float = QUOTE_TIMEOUT_SECONDS
) -> Quote:
    """Stream delayed data until a price arrives or ``timeout`` passes; always unsubscribe."""
    ib.reqMarketDataType(DELAYED_MARKET_DATA)
    ticker = ib.reqMktData(contract)
    try:
        deadline = time.monotonic() + timeout
        while not _quote_from(ticker, symbol, currency).has_price():
            if time.monotonic() >= deadline:
                break
            ib.sleep(0.25)
    finally:
        ib.cancelMktData(contract)
    return _quote_from(ticker, symbol, currency)


def _held_quantity(positions: list[Any], contract: Any, symbol: str, currency: str) -> float:
    """Shares held of ``contract``: matched on conId, else on symbol and currency."""
    con_id = getattr(contract, "conId", 0)
    total = 0.0
    for p in positions:
        same = (
            p.contract.conId == con_id
            if con_id and getattr(p.contract, "conId", 0)
            else p.contract.symbol == symbol and p.contract.currency == currency
        )
        if same:
            total += float(p.position)
    return total


def _quote_from(ticker: Any, symbol: str, currency: str) -> Quote:
    return Quote(
        symbol=symbol,
        currency=currency,
        bid=float(ticker.bid),
        ask=float(ticker.ask),
        last=float(ticker.last),
        close=float(ticker.close),
        market_data_type=int(ticker.marketDataType),
    )


def _preflight_summary(state: Any) -> dict:
    """The whatIf answer worth keeping: margin impact, commission estimate, any warning."""
    commission = getattr(state, "commission", None)
    if isinstance(commission, float) and (not math.isfinite(commission) or commission >= _UNSET):
        commission = None
    return {
        "status": getattr(state, "status", ""),
        "init_margin_change": getattr(state, "initMarginChange", ""),
        "equity_with_loan_change": getattr(state, "equityWithLoanChange", ""),
        "commission": commission,
        "commission_currency": getattr(state, "commissionCurrency", ""),
        "warning": getattr(state, "warningText", ""),
    }
