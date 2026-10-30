"""Pre-trade risk checks. Every order goes through RiskChecker.check() — no exceptions.

The check is pure: the broker gathers everything it needs (a fresh quote, the current position,
what was already sent today, the FX rate, the clock, the halt switch) into a ``RiskContext``
and the checker only decides. That keeps every refusal testable without a gateway.

``RiskLimits()`` defaults are deliberately conservative placeholders. The paper loop sizes
its own from ``Settings`` (``RiskLimits.from_settings``), so loosening them is a conscious,
configured change rather than an edit here.
"""

import math
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from ibkr_trader.execution.broker import OrderRequest

#: US regular trading hours (NYSE/Nasdaq), on the New York wall clock. Exchange holidays
#: are not modelled: an order sent on one waits for the next session.
US_MARKET_TIMEZONE = ZoneInfo("America/New_York")
US_MARKET_OPEN = time(9, 30)
US_MARKET_CLOSE = time(16, 0)


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: float = 1_000.0  # CAD; per single order
    max_daily_notional: float = 5_000.0  # CAD; every order sent in one UTC day
    max_position_per_symbol: float = 2_000.0  # CAD; the position after this order fills
    allowed_sec_types: tuple = ("STK",)  # stocks only for now — no options/futures/forex
    # USD only: IBKR Canada does not allow its Canadian-resident clients to submit API orders
    # for products on a Canadian marketplace (CIRO). See docs/reference/ibkr/04-orders.md.
    allowed_currencies: tuple = ("USD",)
    # How far a live quote may stray from the caller's reference price before the order is
    # refused as a probable wrong-contract match.
    max_price_deviation: float = 0.15

    @classmethod
    def from_settings(cls, settings) -> "RiskLimits":
        return cls(
            max_order_notional=settings.paper_max_order_cad,
            max_daily_notional=settings.paper_max_daily_cad,
            max_position_per_symbol=settings.paper_max_position_cad,
        )


@dataclass(frozen=True)
class RiskContext:
    """What the checker needs to know about the world at the moment of the order."""

    price: float  # a fresh quote, in the order's currency
    fx_to_cad: float  # CAD per unit of the order's currency
    position_quantity: float  # shares held before this order
    traded_today_cad: float  # notional of the orders already sent today
    now: datetime  # timezone-aware
    halted: bool = False


class RiskViolation(RuntimeError):
    pass


def is_us_regular_hours(now: datetime) -> bool:
    """Monday-Friday, 09:30-16:00 New York time (holidays not modelled)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(US_MARKET_TIMEZONE)
    return local.weekday() < 5 and US_MARKET_OPEN <= local.time() < US_MARKET_CLOSE


def _usable(value: float) -> bool:
    return math.isfinite(value) and value > 0


class RiskChecker:
    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()

    def check(self, request: OrderRequest, context: RiskContext) -> None:
        """Raise ``RiskViolation`` naming the first rule the order breaks; else return."""
        if context.halted:
            raise RiskViolation("trading is halted (TRADING_HALTED or the halt file)")
        self.check_shape(request)
        self.check_market(request, context)
        self.check_size(request, context)

    def check_shape(self, request: OrderRequest) -> None:
        """Rules about the order alone: quantity, side, product, listing, order type."""
        limits = self.limits
        if request.quantity <= 0:
            raise RiskViolation("quantity must be positive")
        if request.quantity != int(request.quantity):
            raise RiskViolation("quantity must be a whole number of shares")
        if request.side not in ("BUY", "SELL"):
            raise RiskViolation(f"side must be BUY or SELL, not {request.side!r}")
        if request.sec_type not in limits.allowed_sec_types:
            raise RiskViolation(f"security type {request.sec_type!r} is not allowed")
        if request.currency.upper() not in limits.allowed_currencies:
            raise RiskViolation(
                f"{request.currency} listings are not allowed: IBKR Canada refuses API orders "
                "for Canadian-marketplace products"
            )
        if request.order_type == "LMT":
            if request.limit_price is None or not request.limit_price > 0:
                raise RiskViolation("a limit order needs a positive limit price")
        elif request.order_type != "MKT":
            raise RiskViolation(f"order type {request.order_type!r} is not allowed")

    def check_market(self, request: OrderRequest, context: RiskContext) -> None:
        """Rules about the market right now: a usable quote and rate, the right contract, hours."""
        if not _usable(context.price):
            raise RiskViolation(f"no usable quote for {request.symbol}")
        if not _usable(context.fx_to_cad):
            raise RiskViolation(f"no usable {request.currency}->CAD rate")
        reference = request.reference_price
        if reference is not None and reference > 0:
            drift = abs(context.price / reference - 1.0)
            if drift > self.limits.max_price_deviation:
                raise RiskViolation(
                    f"{request.symbol} quote {context.price:.2f} is {drift:.0%} away from the "
                    f"planned {reference:.2f}: probable wrong contract"
                )
        if request.order_type == "MKT" and not is_us_regular_hours(context.now):
            raise RiskViolation("market orders only during US regular trading hours")

    def check_size(self, request: OrderRequest, context: RiskContext) -> None:
        """Rules about how much: per order, per day, per position, and no short sales."""
        limits = self.limits
        price = context.price
        if request.order_type == "LMT" and request.limit_price is not None:
            price = request.limit_price
        notional = request.quantity * price * context.fx_to_cad
        if notional > limits.max_order_notional:
            raise RiskViolation(
                f"order notional {notional:,.0f} CAD exceeds the "
                f"{limits.max_order_notional:,.0f} CAD per-order cap"
            )
        if context.traded_today_cad + notional > limits.max_daily_notional:
            raise RiskViolation(
                f"today's notional would reach {context.traded_today_cad + notional:,.0f} CAD, "
                f"over the {limits.max_daily_notional:,.0f} CAD daily cap"
            )
        if request.side == "SELL":
            if request.quantity > context.position_quantity:
                raise RiskViolation(
                    f"selling {request.quantity:g} {request.symbol} but only "
                    f"{context.position_quantity:g} held: no short sales"
                )
            return
        after_cad = (context.position_quantity + request.quantity) * price * context.fx_to_cad
        if after_cad > limits.max_position_per_symbol:
            raise RiskViolation(
                f"{request.symbol} position would be {after_cad:,.0f} CAD, over the "
                f"{limits.max_position_per_symbol:,.0f} CAD per-symbol cap"
            )
