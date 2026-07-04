"""Pre-trade risk checks. Every order goes through RiskChecker.check() — no exceptions.

These limits are deliberately conservative placeholders; tighten/loosen consciously,
and IMPLEMENT the TODOs before any live use.
"""

from dataclasses import dataclass

from ibkr_trader.execution.broker import OrderRequest


@dataclass
class RiskLimits:
    max_order_notional: float = 1_000.0  # CAD-ish; per single order
    max_daily_notional: float = 5_000.0
    max_position_per_symbol: float = 2_000.0
    allowed_sec_types: tuple = ("STK",)  # stocks only for now — no options/futures/forex


class RiskViolation(RuntimeError):
    pass


class RiskChecker:
    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()

    def check(self, request: OrderRequest) -> None:
        if request.quantity <= 0:
            raise RiskViolation("quantity must be positive")
        # TODO(skeleton, required before live):
        #  - notional check needs a price: use limit_price or a fresh quote
        #  - daily notional: sum today's orders from the orders table
        #  - position cap: current position + this order
        #  - trading-hours check; halt flag file/env for an emergency stop
