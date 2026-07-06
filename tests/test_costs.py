from ibkr_trader.accounts import AccountType, get_profile
from ibkr_trader.backtest.costs import RegisteredAccountCostModel


def test_trade_cost_applies_min_commission_and_bps():
    model = RegisteredAccountCostModel(
        commission_per_share=0.005,
        min_commission=1.0,
        slippage_bps=5.0,
        spread_bps=2.0,
        churn_penalty_bps=10.0,
    )
    # 10 shares @ $100 = $1000 notional; commission floored at $1, bps = 17bps of $1000 = $1.70
    cost = model.trade_cost(10, 100.0)
    assert abs(cost - (1.0 + 1000 * 0.0017)) < 1e-9


def test_trade_cost_scales_commission_above_the_floor():
    model = RegisteredAccountCostModel(slippage_bps=0, spread_bps=0, churn_penalty_bps=0)
    assert abs(model.trade_cost(1000, 50.0) - 1000 * 0.005) < 1e-9


def test_dividend_withholding_drag_by_account_and_domicile():
    model = RegisteredAccountCostModel(assumed_us_dividend_yield=0.02)
    tfsa = get_profile(AccountType.TFSA)
    rrsp = get_profile(AccountType.RRSP)

    # US holding in a TFSA: 0.02 yield * 0.15 rate / 252
    us_tfsa = model.dividend_tax_drag_daily("US", tfsa)
    assert abs(us_tfsa - (0.02 * 0.15 / 252)) < 1e-12
    # US holding in an RRSP: treaty-exempt
    assert model.dividend_tax_drag_daily("US", rrsp) == 0.0
    # Canadian holding: never withheld, any account
    assert model.dividend_tax_drag_daily("CA", tfsa) == 0.0
