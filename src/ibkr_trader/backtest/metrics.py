"""Performance metrics for backtest equity curves. Pure functions over return series."""

import numpy as np

TRADING_DAYS = 252


def sharpe(daily_returns: np.ndarray, risk_free_daily: float = 0.0) -> float:
    excess = daily_returns - risk_free_daily
    if excess.std() == 0:
        return 0.0
    return float(np.sqrt(TRADING_DAYS) * excess.mean() / excess.std())


def sortino(daily_returns: np.ndarray, risk_free_daily: float = 0.0) -> float:
    """Like Sharpe but penalizes only downside volatility — the risk a long-term holder cares
    about. No losing days → returns 0.0 (undefined upside-only downside)."""
    excess = daily_returns - risk_free_daily
    downside = excess[excess < 0]
    if downside.size == 0 or downside.std() == 0:
        return 0.0
    return float(np.sqrt(TRADING_DAYS) * excess.mean() / downside.std())


def annualized_volatility(daily_returns: np.ndarray) -> float:
    return float(daily_returns.std() * np.sqrt(TRADING_DAYS))


def max_drawdown(equity: np.ndarray) -> float:
    peaks = np.maximum.accumulate(equity)
    drawdowns = (equity - peaks) / peaks
    return float(drawdowns.min())


def cagr(equity: np.ndarray, n_days: int) -> float:
    if n_days <= 0 or equity[0] <= 0:
        return 0.0
    years = n_days / TRADING_DAYS
    return float((equity[-1] / equity[0]) ** (1 / years) - 1)


def calmar(equity: np.ndarray, n_days: int) -> float:
    """CAGR per unit of worst drawdown — reward for the pain endured. 0 if no drawdown."""
    dd = max_drawdown(equity)
    if dd == 0:
        return 0.0
    return float(cagr(equity, n_days) / abs(dd))


def total_return(equity: np.ndarray) -> float:
    if equity[0] <= 0:
        return 0.0
    return float(equity[-1] / equity[0] - 1)


def summarize(equity: np.ndarray) -> dict:
    """Standard metric bundle for one equity curve. Keys are stable — the leaderboard and
    ``backtest compare`` read them straight out of ``backtest_runs.metrics``."""
    returns = np.diff(equity) / equity[:-1]
    return {
        "sharpe": sharpe(returns),
        "sortino": sortino(returns),
        "annual_vol": annualized_volatility(returns),
        "max_drawdown": max_drawdown(equity),
        "cagr": cagr(equity, len(equity)),
        "calmar": calmar(equity, len(equity)),
        "total_return": total_return(equity),
        "n_days": len(equity),
    }
