"""Compare persisted backtest runs head-to-head — the model leaderboard.

Pure DB reads over ``backtest_runs`` (no network, no engine): "which model won over this
window?". Fair comparison is the caller's responsibility — only rank runs that shared a
universe, window and cost model (the engine bakes in the no-look-ahead guardrails; see
backtest.engine). Pin ``model_version`` and ``horizon`` into each run's ``params`` so a row on
the leaderboard maps back to exactly which trained model produced it.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ibkr_trader.db.models import BacktestRun


def compare_runs(
    session: Session,
    *,
    strategy: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    sort_by: str = "sharpe",
    descending: bool = True,
    limit: int | None = None,
) -> list[BacktestRun]:
    """Return backtest runs ranked by a metric key drawn from their ``metrics`` JSON.

    Optional filters: exact ``strategy`` match, and runs contained within ``[start, end]``.
    Ranking happens in Python (metrics is a JSON blob, not columns) so it is portable across
    Postgres and the SQLite used in tests. Runs missing ``sort_by`` sort to the bottom in
    either direction, so a broken run never masquerades as the winner.
    """
    stmt = select(BacktestRun)
    if strategy is not None:
        stmt = stmt.where(BacktestRun.strategy == strategy)
    if start is not None:
        stmt = stmt.where(BacktestRun.start >= start)
    if end is not None:
        stmt = stmt.where(BacktestRun.end <= end)
    runs = list(session.scalars(stmt))

    present = [run for run in runs if (run.metrics or {}).get(sort_by) is not None]
    missing = [run for run in runs if (run.metrics or {}).get(sort_by) is None]
    # `present` is filtered to runs whose metrics hold a non-None sort_by, so `or {}` is only
    # here to satisfy the type checker (metrics is Mapped[dict | None]); the key always exists.
    present.sort(key=lambda run: (run.metrics or {})[sort_by], reverse=descending)
    ranked = present + missing
    return ranked[:limit] if limit else ranked
