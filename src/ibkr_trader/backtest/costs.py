"""Cost & tax model for the registered-account long-term simulator.

This is where the "limits" become dollars — the cost function that makes the model *want* to
trade rarely and hold boring, sheltered things:

- **Transaction cost** on every simulated fill: commission + slippage + half-spread. Standard.
- **Churn penalty**: an *extra* soft cost on traded notional, on top of real transaction cost.
  It represents the compounding downside of over-trading (mistakes, tax attention, drift from a
  long-term plan) and is the knob that does the heavy lifting — raise it and the allocator's
  small rebalances stop being worth it long before the hard annual trade cap bites (that cap is
  the backstop, enforced by the engine/risk check, not this model).
- **Dividend withholding drag**: non-recoverable US-dividend withholding for the account type,
  charged only against US-domiciled holdings (Canadian dividends are never withheld). See
  ibkr_trader.accounts for the per-account rates and the treaty caveats.
- **FX conversion cost**: the account is funded in CAD, so shifting the CAD↔USD mix (e.g. going
  from 50/50 to 100% US) pays a currency-conversion spread on the net amount crossing the
  boundary. Reshuffling *within* a currency (US name → US name) costs nothing here.

All amounts are in the account's reporting currency (CAD): the engine converts native-currency
prices to CAD before calling in here.
"""

from dataclasses import dataclass

from ibkr_trader.accounts import AccountTaxProfile


@dataclass
class RegisteredAccountCostModel:
    commission_per_share: float = 0.005  # CAD-ish; verify vs IBKR Canada fee schedule
    min_commission: float = 1.0
    slippage_bps: float = 5.0
    spread_bps: float = 2.0  # half-spread paid crossing the book
    churn_penalty_bps: float = 10.0  # extra turnover deterrent (the primary low-turnover lever)
    fx_conversion_bps: float = 20.0  # spread paid converting CAD↔USD (IBKR itself is far cheaper)
    assumed_us_dividend_yield: float = 0.015  # gross annual yield assumed for US holdings

    def trade_cost(self, shares: float, price_cad: float) -> float:
        """Total cost (CAD) of trading ``shares`` at ``price_cad`` per share."""
        notional = abs(shares) * price_cad
        commission = max(self.min_commission, abs(shares) * self.commission_per_share)
        rate = (self.slippage_bps + self.spread_bps + self.churn_penalty_bps) / 10_000
        return commission + notional * rate

    def fx_conversion_cost(self, net_crossing_cad: float) -> float:
        """Cost (CAD) of converting ``net_crossing_cad`` (abs) of currency across CAD↔USD."""
        return abs(net_crossing_cad) * self.fx_conversion_bps / 10_000

    def dividend_tax_drag_daily(
        self, domicile: str, profile: AccountTaxProfile, periods_per_year: int = 252
    ) -> float:
        """Per-period return haircut from non-recoverable US-dividend withholding.

        Approximation: total-return (ADJUSTED_LAST) bars already reinvest dividends gross, so we
        subtract the withheld slice as a smooth daily drag on US-domiciled holdings
        (``yield × effective_rate / periods``). Precise per-security dividend cashflows are
        future work once dividend data is ingested. Non-US holdings and treaty-exempt accounts
        return 0.
        """
        if domicile != "US":
            return 0.0
        annual_drag = self.assumed_us_dividend_yield * profile.effective_us_withholding()
        return annual_drag / periods_per_year
