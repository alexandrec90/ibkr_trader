"""RiskChecker sits in every order path (hard rule #1) — pin the allowed path, the refused
paths, and the deliberately-conservative default limits so loosening any of them is a
conscious, test-visible change."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from ibkr_trader.config import Settings
from ibkr_trader.execution.broker import OrderRequest
from ibkr_trader.execution.risk import (
    RiskChecker,
    RiskContext,
    RiskLimits,
    RiskViolation,
    is_us_regular_hours,
)

#: Tuesday 2026-10-27 11:00 New York (15:00 UTC, EDT).
OPEN = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)

LIMITS = RiskLimits(
    max_order_notional=25_000.0, max_daily_notional=210_000.0, max_position_per_symbol=30_000.0
)


def _order(quantity: float = 10, **overrides) -> OrderRequest:
    values = {"symbol": "AAPL", "side": "BUY", "quantity": quantity}
    values.update(overrides)
    return OrderRequest(**values)


def _context(**overrides) -> RiskContext:
    values = {
        "price": 100.0,
        "fx_to_cad": 1.4,
        "position_quantity": 0.0,
        "traded_today_cad": 0.0,
        "now": OPEN,
    }
    values.update(overrides)
    return RiskContext(**values)


def _check(request: OrderRequest | None = None, context: RiskContext | None = None) -> None:
    RiskChecker(LIMITS).check(request or _order(), context or _context())


def test_valid_order_passes():
    _check()  # 10 x 100 USD x 1.4 = 1,400 CAD


def test_zero_quantity_refused():
    with pytest.raises(RiskViolation, match="quantity must be positive"):
        _check(_order(quantity=0))


def test_negative_quantity_refused():
    with pytest.raises(RiskViolation):
        _check(_order(quantity=-5))


def test_fractional_quantity_refused():
    with pytest.raises(RiskViolation, match="whole number"):
        _check(_order(quantity=1.5))


def test_halt_refuses_everything():
    with pytest.raises(RiskViolation, match="halted"):
        _check(context=_context(halted=True))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"side": "SHORT"}, "side must be BUY or SELL"),
        ({"sec_type": "OPT"}, "security type"),
        ({"order_type": "STP"}, "order type"),
        ({"order_type": "LMT"}, "positive limit price"),
        ({"order_type": "LMT", "limit_price": 0.0}, "positive limit price"),
    ],
)
def test_malformed_orders_refused(overrides, message):
    with pytest.raises(RiskViolation, match=message):
        _check(_order(**overrides))


def test_canadian_listing_refused():
    """IBKR Canada refuses API orders for Canadian-marketplace products (CIRO)."""
    with pytest.raises(RiskViolation, match="Canadian-marketplace"):
        _check(_order(symbol="XEQT", currency="CAD"), _context(fx_to_cad=1.0))


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan")])
def test_missing_quote_refused(price):
    with pytest.raises(RiskViolation, match="no usable quote"):
        _check(context=_context(price=price))


@pytest.mark.parametrize("fx", [0.0, float("nan")])
def test_missing_fx_refused(fx):
    with pytest.raises(RiskViolation, match="CAD rate"):
        _check(context=_context(fx_to_cad=fx))


def test_quote_far_from_reference_is_a_probable_wrong_contract():
    with pytest.raises(RiskViolation, match="wrong contract"):
        _check(_order(reference_price=50.0))
    _check(_order(reference_price=90.0))  # 11% away: inside the 15% band


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 27, 13, 0, tzinfo=UTC),  # 09:00 New York, before the open
        datetime(2026, 10, 27, 20, 0, tzinfo=UTC),  # 16:00 New York, at the close
        datetime(2026, 10, 31, 15, 0, tzinfo=UTC),  # Saturday
    ],
)
def test_market_orders_only_in_regular_hours(now):
    with pytest.raises(RiskViolation, match="regular trading hours"):
        _check(context=_context(now=now))


def test_limit_orders_may_rest_outside_hours():
    _check(
        _order(order_type="LMT", limit_price=100.0),
        _context(now=datetime(2026, 10, 27, 22, 0, tzinfo=UTC)),
    )


def test_per_order_cap():
    # 179 x 100 x 1.4 = 25,060 CAD
    with pytest.raises(RiskViolation, match="per-order cap"):
        _check(_order(quantity=179))
    _check(_order(quantity=178))  # 24,920 CAD


def test_limit_price_is_what_the_cap_measures():
    with pytest.raises(RiskViolation, match="per-order cap"):
        _check(_order(quantity=100, order_type="LMT", limit_price=200.0))


def test_daily_cap_counts_what_was_already_sent():
    with pytest.raises(RiskViolation, match="daily cap"):
        _check(context=_context(traded_today_cad=209_000.0))
    _check(context=_context(traded_today_cad=208_000.0))


def test_no_short_sales():
    with pytest.raises(RiskViolation, match="no short sales"):
        _check(_order(side="SELL", quantity=11), _context(position_quantity=10))
    _check(_order(side="SELL", quantity=10), _context(position_quantity=10))


def test_position_cap_counts_the_existing_position():
    # (200 + 15) x 100 x 1.4 = 30,100 CAD
    with pytest.raises(RiskViolation, match="per-symbol cap"):
        _check(_order(quantity=15), _context(position_quantity=200))
    _check(_order(quantity=14), _context(position_quantity=200))


def test_a_sell_is_not_held_to_the_position_cap():
    _check(_order(side="SELL", quantity=10), _context(position_quantity=1_000))


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
    assert limits.allowed_currencies == ("USD",)  # no Canadian listings through the API


def test_custom_limits_are_used():
    checker = RiskChecker(RiskLimits(max_order_notional=50.0))
    assert checker.limits.max_order_notional == 50.0
    with pytest.raises(RiskViolation, match="per-order cap"):
        checker.check(_order(quantity=1), _context())


def test_paper_limits_come_from_settings_sized_to_the_sleeve():
    settings = Settings(_env_file=None)
    limits = RiskLimits.from_settings(settings)
    assert settings.paper_capital_cad == 100_000.0
    assert limits.max_order_notional == settings.paper_max_order_cad == 25_000.0
    assert limits.max_daily_notional == settings.paper_max_daily_cad == 210_000.0
    assert limits.max_position_per_symbol == settings.paper_max_position_cad == 30_000.0
    assert limits.allowed_currencies == ("USD",)
    # one 20%-cap position of the 100k sleeve must fit a single order
    assert limits.max_order_notional >= 0.20 * settings.paper_capital_cad
    custom = replace(limits, max_order_notional=1.0)
    assert custom.max_daily_notional == limits.max_daily_notional


def test_the_rule_groups_can_be_checked_alone():
    checker = RiskChecker(LIMITS)

    checker.check_shape(_order())
    with pytest.raises(RiskViolation, match="whole number"):
        checker.check_shape(_order(quantity=0.5))

    checker.check_market(_order(), _context())
    with pytest.raises(RiskViolation, match="no usable quote"):
        checker.check_market(_order(), _context(price=0.0))

    checker.check_size(_order(), _context())
    with pytest.raises(RiskViolation, match="no short sales"):
        checker.check_size(_order(side="SELL"), _context(position_quantity=0))


def test_regular_hours_needs_an_aware_clock():
    assert is_us_regular_hours(OPEN)
    with pytest.raises(ValueError, match="timezone-aware"):
        is_us_regular_hours(datetime(2026, 10, 27, 11, 0))
