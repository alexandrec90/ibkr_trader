"""What each UI page shows, as JSON-ready data. DB/filesystem in, plain dicts out.

No FastAPI import, so these are unit-tested against in-memory SQLite without the ``[ui]``
extra (tests/test_web_views.py); ``app.py`` only routes to them. Every function is
read-only: the one page that does anything (a Questrade plan) goes through
``advisory.service``, which never transmits an order.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ibkr_trader.dashboard import data as dash
from ibkr_trader.db.models import Execution, Instrument, Order, StrategySnapshot

#: Realization horizons the paper page scores forward snapshots over, in months.
SHADOW_HORIZONS = (1, 3, 6, 12)
#: Most backtest runs one chart overlays: the categorical palette's eight slots.
MAX_CURVES = 8
#: Most dates a curve is drawn at; about one per pixel of a wide chart.
MAX_POINTS = 1000


def _clean(value: Any) -> Any:
    """A value ``json.dumps`` accepts with ``allow_nan=False``: NaN/inf become None."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, datetime | date):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalar
        return _clean(value.item())
    return value


def backtest_runs(session: Session) -> list[dict[str, Any]]:
    """The leaderboard: one row per persisted backtest run, newest first."""
    frame = dash.runs_frame(session)
    return [
        {key: _clean(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _date_grid(dates: list[date], max_points: int) -> set[date]:
    """At most ``max_points`` of ``dates``, evenly strided, always keeping the last.

    One grid for every series, so the sampled curves still share their x values; sampling
    each series on its own would leave the chart's rows half-empty.
    """
    ordered = sorted(set(dates))
    stride = max(1, math.ceil(len(ordered) / max_points))
    return set(ordered[::stride]) | {ordered[-1]}


def backtest_curves(
    session: Session, run_ids: list[int], *, max_points: int = MAX_POINTS
) -> list[dict[str, Any]]:
    """Growth-of-100 and drawdown series for each selected run and its benchmark.

    Rebased so runs with different capital or windows overlay on one axis, and sampled
    to at most ``max_points`` shared dates: daily since 2010 is four points per pixel,
    and drawing them all made hovering stall. Drawdowns are computed before sampling,
    so the curve's shape is exact at every date shown; the exact maximum drawdown stays
    in the leaderboard. A benchmark identical to one already listed (runs over the same
    window) is listed once. A run stored before curves were persisted contributes nothing.
    """
    if len(run_ids) > MAX_CURVES:
        raise ValueError(f"at most {MAX_CURVES} runs can be compared at once")
    curves = dash.curve_frame(session, run_ids)
    if curves.empty:
        return []
    framed = dash.drawdown(dash.rebase(curves))
    grid = _date_grid(list(framed["date"]), max_points)
    out = []
    benchmarks: set[tuple] = set()
    for (series, kind, run_id), group in framed.groupby(["series", "kind", "run_id"], sort=False):
        group = group.sort_values("date")
        if kind == "benchmark":
            fingerprint = tuple(zip(group["date"], group["equity_cad"], strict=True))
            if fingerprint in benchmarks:
                continue
            benchmarks.add(fingerprint)
        group = group[group["date"].isin(grid)]
        out.append(
            {
                "run_id": int(run_id),
                "label": series,
                "kind": kind,
                "points": [
                    [d.isoformat(), round(float(v), 4), round(float(dd), 6)]
                    for d, v, dd in zip(
                        group["date"], group["equity_cad"], group["drawdown"], strict=True
                    )
                ],
            }
        )
    return out


def trained_model(models_dir: Path) -> dict[str, Any] | None:
    """The latest ML artifact's identity and walk-forward ICs, or None before any training."""
    from ibkr_trader.signals.train import load_latest_metadata

    try:
        metadata = load_latest_metadata(models_dir)
    except FileNotFoundError:
        return None
    validation = metadata.get("validation") or {}
    universe = metadata.get("universe") or {}
    window = metadata.get("train_window") or {}
    return {
        "model": metadata.get("model"),
        "version": metadata.get("version"),
        "created_at": metadata.get("created_at"),
        "feature_set_version": metadata.get("feature_set_version"),
        "label": (metadata.get("label") or {}).get("spec"),
        "dataset": {
            "rows": window.get("n_rows"),
            "dates": window.get("n_dates"),
            "span": window.get("dataset_dates"),
            "symbols": universe.get("n_symbols"),
            "survivorship": universe.get("survivorship", "curated-current"),
        },
        "scheme": validation.get("scheme"),
        "purge_months": validation.get("purge_months"),
        "folds": [
            {
                "fold": fold.get("fold_id"),
                "train": fold.get("train"),
                "test": fold.get("test"),
                "ic": {name: (ic or {}).get("mean") for name, ic in (fold.get("ic") or {}).items()},
            }
            for fold in validation.get("folds", [])
        ],
        "overall_ic": {
            name: {"mean": (ic or {}).get("mean"), "std": (ic or {}).get("std")}
            for name, ic in (validation.get("overall_ic") or {}).items()
        },
    }


def _symbols(session: Session, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = session.execute(select(Instrument.id, Instrument.symbol).where(Instrument.id.in_(ids)))
    return {iid: symbol for iid, symbol in rows}


def _turnover(current: dict[str, float], previous: dict[str, float] | None) -> float | None:
    """One-way turnover between two weight books: half the summed absolute change."""
    if previous is None:
        return None
    keys = set(current) | set(previous)
    return sum(abs(current.get(k, 0.0) - previous.get(k, 0.0)) for k in keys) / 2


def shadow_books(session: Session) -> list[dict[str, Any]]:
    """Every strategy's forward-snapshot history and its latest book, by symbol."""
    snapshots = list(
        session.scalars(
            select(StrategySnapshot).order_by(StrategySnapshot.strategy, StrategySnapshot.ts)
        )
    )
    ids = {int(iid) for row in snapshots for iid in row.weights}
    symbols = _symbols(session, ids)
    books: dict[str, dict[str, Any]] = {}
    previous: dict[str, dict[str, float]] = {}
    for row in snapshots:
        weights = {str(k): float(v) for k, v in row.weights.items()}
        book = books.setdefault(row.strategy, {"strategy": row.strategy, "history": []})
        book["history"].append(
            {
                "asof": row.ts.date().isoformat(),
                "model_version": row.model_version,
                "names": len(weights),
                "invested": sum(weights.values()),
                "turnover": _turnover(weights, previous.get(row.strategy)),
            }
        )
        previous[row.strategy] = weights
        book["latest"] = {
            "asof": row.ts.date().isoformat(),
            "model_version": row.model_version,
            "bar_asof": (row.params or {}).get("bar_asof"),
            "holdings": sorted(
                (
                    {"symbol": symbols.get(int(iid), f"#{iid}"), "weight": weight}
                    for iid, weight in weights.items()
                ),
                key=lambda h: -h["weight"],
            ),
        }
    return list(books.values())


def shadow_performance(
    session: Session, *, asof: date | None = None, horizons: tuple[int, ...] = SHADOW_HORIZONS
) -> dict[str, Any]:
    """Realized forward outcomes per horizon, net of turnover costs, versus XEQT.

    ``error`` carries the reason scoring was impossible (no XEQT bars) instead of raising,
    so the books still render beside it.
    """
    from ibkr_trader.backtest.snapshot import snapshot_outcomes

    asof = asof or datetime.now(tz=UTC).date()
    result: dict[str, Any] = {"asof": asof.isoformat(), "horizons": [], "error": None}
    for months in horizons:
        try:
            outcomes = snapshot_outcomes(session, horizon_months=months, asof=asof)
        except ValueError as exc:
            result["error"] = str(exc)
            break
        result["horizons"].append(
            {
                "months": months,
                "outcomes": [
                    {
                        "strategy": o.strategy,
                        "asof": o.asof.isoformat(),
                        "horizon_end": o.horizon_end.isoformat(),
                        "portfolio_return": o.portfolio_return,
                        "cost_return": o.cost_return,
                        "benchmark_return": o.benchmark_return,
                        "excess": o.excess,
                    }
                    for o in outcomes
                ],
            }
        )
    return result


def paper_orders(session: Session, *, limit: int = 200) -> dict[str, Any]:
    """IBKR orders this service recorded, newest first, with their fill totals.

    Empty until the paper loop in ``serve`` exists (TODO.md §4); the page says so.
    """
    fills = (
        select(
            Execution.order_id,
            func.count(Execution.id).label("fills"),
            func.sum(Execution.commission).label("commission"),
        )
        .group_by(Execution.order_id)
        .subquery()
    )
    rows = session.execute(
        select(Order, Instrument.symbol, fills.c.fills, fills.c.commission)
        .join(Instrument, Instrument.id == Order.instrument_id)
        .outerjoin(fills, fills.c.order_id == Order.id)
        .order_by(Order.placed_at.desc().nulls_last(), Order.id.desc())
        .limit(limit)
    )
    orders = [
        {
            "id": order.id,
            "environment": order.environment,
            "symbol": symbol,
            "side": order.side.value,
            "quantity": order.quantity,
            "order_type": order.order_type,
            "limit_price": order.limit_price,
            "status": order.status,
            "placed_at": _clean(order.placed_at),
            "avg_fill_price": order.avg_fill_price,
            "filled_quantity": order.filled_quantity,
            "fills": n_fills or 0,
            "commission": commission,
        }
        for order, symbol, n_fills, commission in rows
    ]
    return {"orders": orders}


def snapshot_strategies(session: Session) -> list[str]:
    """Strategies with at least one forward snapshot: the ones a Questrade plan can follow."""
    return list(
        session.scalars(
            select(StrategySnapshot.strategy).distinct().order_by(StrategySnapshot.strategy)
        )
    )


def latest_plan(path: Path) -> dict[str, Any] | None:
    """The last Questrade plan written by the CLI or the UI, or None if there is none."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not a readable plan: {exc}") from exc


def report_files(reports_dir: Path) -> list[dict[str, Any]]:
    """Generated HTML reports (lab, backtest results, factor tear sheets), newest first."""
    if not reports_dir.is_dir():
        return []
    files = [p for p in reports_dir.rglob("*.html") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "path": p.relative_to(reports_dir).as_posix(),
            "modified": datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat(),
        }
        for p in files
    ]
