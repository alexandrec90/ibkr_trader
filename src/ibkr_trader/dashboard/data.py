"""Data access for the dashboard — pure DB-in/frame-out helpers, no streamlit imports.

Everything here is unit-testable against an in-memory SQLite session; app.py stays a thin
rendering shell. Equity curves come from the ``equity_curve`` / ``benchmark_equity_curve``
lists the engine persists into ``backtest_runs.metrics`` (runs recorded before that feature
simply have no plottable curve — ``runs_frame`` flags them via ``has_curve``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ibkr_trader.db.models import BacktestRun

#: Scalar metrics surfaced as leaderboard columns, in display order.
SUMMARY_METRICS = (
    "total_return",
    "benchmark_total_return",
    "excess_return",
    "cagr",
    "sharpe",
    "sortino",
    "max_drawdown",
    "calmar",
    "n_trades",
    "trades_per_year",
    "costs_cad",
    "tax_cad",
    "fx_cost_cad",
    "end_value_cad",
)


def runs_frame(session: Session) -> pd.DataFrame:
    """One row per persisted backtest run, newest first — the leaderboard's raw material."""
    rows = []
    for run in session.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc())):
        metrics = run.metrics or {}
        params = run.params or {}
        universe = params.get("universe") or {}
        rows.append(
            {
                "run_id": run.id,
                "strategy": run.strategy,
                "account": params.get("account", "?"),
                "window": f"{run.start:%Y-%m-%d} → {run.end:%Y-%m-%d}",
                "created_at": run.created_at,
                "model_version": params.get("model_version"),
                "universe_n": universe.get("n_symbols"),
                "universe_hash": universe.get("sha256_16"),
                "benchmark": params.get("benchmark"),
                "has_curve": bool(metrics.get("equity_curve")),
                **{key: metrics.get(key) for key in SUMMARY_METRICS},
            }
        )
    return pd.DataFrame(rows)


def run_label(run_id: int, strategy: str, account: str, window: str) -> str:
    """Stable human-readable identity for a run across the picker and the charts."""
    return f"#{run_id} {strategy} · {account} · {window}"


def curve_frame(session: Session, run_ids: list[int]) -> pd.DataFrame:
    """Long-format equity curves for the selected runs: date, equity_cad, series, kind.

    ``kind`` is "strategy" or "benchmark" so the chart can style the reference series
    differently. Each run contributes its own benchmark curve (identical windows will
    overlap harmlessly). Runs persisted before curve storage contribute nothing.
    """
    frames: list[pd.DataFrame] = []
    for run in session.scalars(select(BacktestRun).where(BacktestRun.id.in_(run_ids))):
        metrics = run.metrics or {}
        params = run.params or {}
        window = f"{run.start:%Y-%m-%d} → {run.end:%Y-%m-%d}"
        label = run_label(run.id, run.strategy, params.get("account", "?"), window)
        for key, kind, series in (
            ("equity_curve", "strategy", label),
            (
                "benchmark_equity_curve",
                "benchmark",
                f"{params.get('benchmark', 'benchmark')} #{run.id}",
            ),
        ):
            curve = metrics.get(key) or []
            if not curve:
                continue
            frames.append(
                pd.DataFrame(
                    {
                        "date": [date.fromisoformat(row[0]) for row in curve],
                        "equity_cad": [float(row[1]) for row in curve],
                        "series": series,
                        "kind": kind,
                        "run_id": run.id,
                    }
                )
            )
    if not frames:
        return pd.DataFrame(columns=["date", "equity_cad", "series", "kind", "run_id"])
    return pd.concat(frames, ignore_index=True)


def rebase(curves: pd.DataFrame, base: float = 100.0) -> pd.DataFrame:
    """Rebase every series to ``base`` at its own first point (growth-of-100 view), so runs
    with different starting capital or windows overlay comparably."""
    if curves.empty:
        return curves
    out = curves.copy()
    firsts = out.sort_values("date").groupby("series")["equity_cad"].transform("first")
    out["equity_cad"] = out["equity_cad"] / firsts * base
    return out


def drawdown(curves: pd.DataFrame) -> pd.DataFrame:
    """Per-series running drawdown (fraction ≤ 0) from a ``curve_frame``-shaped frame."""
    if curves.empty:
        return curves.assign(drawdown=pd.Series(dtype=float))
    out = curves.sort_values(["series", "date"]).copy()
    peaks = out.groupby("series")["equity_cad"].cummax()
    out["drawdown"] = out["equity_cad"] / peaks - 1.0
    return out


def read_universe_file(path: str) -> list[str]:
    """One symbol per line, uppercased — same contract as the CLI's --universe-file."""
    try:
        with open(path, encoding="utf-8") as handle:
            universe = [line.strip().upper() for line in handle if line.strip()]
    except OSError as exc:
        raise ValueError(f"cannot read {path!r}: {exc}") from None
    if not universe:
        raise ValueError(f"empty universe file {path!r}")
    return universe


def run_backtest(
    strategy: str,
    universe_file: str,
    account: str,
    start: date,
    end: date,
    *,
    eval_start: date | None = None,
    start_capital: float = 100_000.0,
    min_history_days: int = 252,
):
    """Launch one persisted backtest with the same settings-driven config as the CLI.

    Returns the engine's ``BacktestResult``. Raises ValueError for bad inputs (unknown
    strategy, unreadable universe, no bars) — the app surfaces the message as-is.
    """
    from ibkr_trader.accounts import AccountType
    from ibkr_trader.backtest.costs import RegisteredAccountCostModel
    from ibkr_trader.backtest.engine import BacktestEngine, RegisteredStrategyConfig
    from ibkr_trader.config import get_settings
    from ibkr_trader.signals.eligibility import EligibilityLimits
    from ibkr_trader.signals.portfolio import get_allocator

    try:
        get_allocator(strategy)  # fail fast with the registry's message
    except (KeyError, RuntimeError, FileNotFoundError) as exc:
        raise ValueError(str(exc)) from None
    if eval_start is not None and eval_start <= start:
        raise ValueError("eval start must be after the window start (it needs warm-up bars)")

    settings = get_settings()
    config = RegisteredStrategyConfig(
        account=AccountType(account.lower()),
        start_capital=start_capital,
        annual_trade_budget=settings.annual_trade_budget,
        rebalance_band=settings.rebalance_band,
        benchmark_symbol=settings.benchmark_symbol,
        eligibility=EligibilityLimits(min_history_days=min_history_days),
        eval_start=eval_start,
    )
    cost_model = RegisteredAccountCostModel(
        churn_penalty_bps=settings.churn_penalty_bps,
        fx_conversion_bps=settings.fx_conversion_bps,
        assumed_us_dividend_yield=settings.us_dividend_yield_assumption,
    )
    return BacktestEngine(cost_model=cost_model).run(
        strategy,
        read_universe_file(universe_file),
        datetime.combine(start, datetime.min.time(), tzinfo=UTC),
        datetime.combine(end, datetime.min.time(), tzinfo=UTC),
        config=config,
    )
