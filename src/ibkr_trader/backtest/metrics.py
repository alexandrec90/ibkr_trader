"""Performance metrics for backtest equity curves. Pure functions over return series."""

import numpy as np

TRADING_DAYS = 252


def sharpe(daily_returns: np.ndarray, risk_free_daily: float = 0.0) -> float:
    excess = daily_returns - risk_free_daily
    if excess.std() == 0:
        return 0.0
    return float(np.sqrt(TRADING_DAYS) * excess.mean() / excess.std())


def max_drawdown(equity: np.ndarray) -> float:
    peaks = np.maximum.accumulate(equity)
    drawdowns = (equity - peaks) / peaks
    return float(drawdowns.min())


def cagr(equity: np.ndarray, n_days: int) -> float:
    if n_days <= 0 or equity[0] <= 0:
        return 0.0
    years = n_days / TRADING_DAYS
    return float((equity[-1] / equity[0]) ** (1 / years) - 1)


def summarize(equity: np.ndarray) -> dict:
    returns = np.diff(equity) / equity[:-1]
    return {
        "sharpe": sharpe(returns),
        "max_drawdown": max_drawdown(equity),
        "cagr": cagr(equity, len(equity)),
        "n_days": len(equity),
    }
