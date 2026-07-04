"""Backtest engine skeleton.

Design goals:
- Reads bars/predictions ONLY from Postgres (no network) → reproducible runs.
- Costs are first-class: commission + slippage models applied to every simulated fill,
  because sentiment strategies live or die on trading costs.
- Results persisted to `backtest_runs` (params + metrics JSON) for comparison over time.

Guardrails against classic backtest lies:
- Signals computed at t may only trade at t+1's open (no look-ahead).
- Use ADJUSTED_LAST bars for returns, raw TRADES for volume-based features.
- Universe selection must not condition on the future (survivorship bias — IBKR has no
  delisted-ticker history; see docs/ibkr/03-market-data-and-historical.md).
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CostModel:
    commission_per_share: float = 0.005  # IBKR-ish; verify against your fee schedule
    min_commission: float = 1.0
    slippage_bps: float = 5.0

    def cost(self, quantity: float, price: float) -> float:
        commission = max(self.min_commission, abs(quantity) * self.commission_per_share)
        slippage = abs(quantity) * price * self.slippage_bps / 10_000
        return commission + slippage


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    start: datetime
    end: datetime
    equity_curve: list = field(default_factory=list)  # (ts, equity) pairs
    metrics: dict = field(default_factory=dict)


class BacktestEngine:
    def __init__(self, cost_model: CostModel | None = None):
        self.cost_model = cost_model or CostModel()

    def run(self, strategy: str, symbols: list[str], start: datetime, end: datetime,
            params: dict | None = None) -> BacktestResult:
        # TODO(skeleton):
        #   1. load bars for symbols from price_bars (daily first; intraday later)
        #   2. iterate days: compute/lookup signal as-of close(t), trade at open(t+1)
        #   3. apply CostModel to fills, track cash/positions/equity
        #   4. metrics via backtest.metrics; persist BacktestRun row
        raise NotImplementedError
