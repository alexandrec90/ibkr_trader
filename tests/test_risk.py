"""RiskChecker sits in every order path (hard rule #1) — pin the allowed path, the refused
paths, and the deliberately-conservative default limits so loosening any of them is a
conscious, test-visible change."""

import pytest

from ibkr_trader.execution.broker import OrderRequest
from ibkr_trader.execution.risk import RiskChecker, RiskLimits, RiskViolation


def _order(quantity: float) -> OrderRequest:
    return OrderRequest(symbol="AAPL", side="BUY", quantity=quantity)


def test_valid_order_passes():
    RiskChecker().check(_order(quantity=10))


def test_zero_quantity_refused():
    with pytest.raises(RiskViolation, match="quantity must be positive"):
        RiskChecker().check(_order(quantity=0))


def test_negative_quantity_refused():
    with pytest.raises(RiskViolation):
        RiskChecker().check(_order(quantity=-5))


def test_risk_violation_is_a_runtime_error():
    # order paths that guard with `except RuntimeError` must still catch violations
    assert issubclass(RiskViolation, RuntimeError)


def test_default_limits_stay_conservative():
    """risk.py calls these "deliberately conservative placeholders" — hold it to that."""
    limits = RiskLimits()
    assert limits.max_order_notional <= 1_000.0
    assert limits.max_daily_notional <= 5_000.0
    assert limits.max_position_per_symbol <= 2_000.0
    assert limits.allowed_sec_types == ("STK",)  # stocks only — no options/futures/forex


def test_custom_limits_are_used():
    checker = RiskChecker(RiskLimits(max_order_notional=50.0))
    assert checker.limits.max_order_notional == 50.0
