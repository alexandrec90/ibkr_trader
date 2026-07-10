"""Supervised dataset for the long-term model (ML-03).

One row per (instrument, monthly rebalance date t): current feature set as-of t, the eligibility
screen as-of t, and the label fixed by prior decision — **12-month forward total return in CAD
(ADJUSTED_LAST bars, dividends reinvested), in excess of XEQT, percentile-ranked
cross-sectionally per rebalance date** into [0, 1].

Layout mirrors signals.features: a pure core (``build_dataset_from_panel``) over in-memory
``Series`` panels so the no-look-ahead and rank-label properties are unit-testable without a
database, plus a thin Postgres wrapper (``build_dataset``) that loads bars through the engine's
one-source-per-instrument loader. No network anywhere.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pandas as pd
from sqlalchemy.orm import Session

from ibkr_trader.backtest.engine import (
    RegisteredStrategyConfig,
    Series,
    _build_candidates,
    _features_asof,
    _fx_to_cad,
    _load_series,
    _load_universe,
)
from ibkr_trader.signals.eligibility import EligibilityLimits, screen
from ibkr_trader.signals.features import CorporateData, load_corporate_inputs

#: The label's forward horizon, in monthly rebalance periods. Walk-forward validation must
#: purge at least this many months between a train window's end and its test window's start.
LABEL_HORIZON_MONTHS = 12

#: Non-feature columns. ``fwd_excess_return`` is the raw (un-ranked) excess return kept for
#: diagnostics — it is *forward-looking* and must never be fed to a model as a feature;
#: anything not listed here is a feature (numeric, plus the categorical ``sector``).
META_COLUMNS = ("instrument_id", "symbol", "date", "fwd_excess_return", "label")

#: A label endpoint's as-of bar must be at most this many calendar days stale; older means the
#: name stopped trading (delisting, ingestion hole) and its "return" would be fiction.
_MAX_STALE_DAYS = 20


def feature_columns(df: pd.DataFrame) -> list[str]:
    """The model-input columns of a dataset frame (everything that isn't meta/label)."""
    return [column for column in df.columns if column not in META_COLUMNS]


def month_end_rebalance_dates(calendar: Sequence[date]) -> list[date]:
    """Last trading date of each calendar month present in ``calendar`` (sorted)."""
    last_by_month: dict[tuple[int, int], date] = {}
    for day in calendar:
        key = (day.year, day.month)
        if key not in last_by_month or day > last_by_month[key]:
            last_by_month[key] = day
    return sorted(last_by_month.values())


def _cad_close_asof(series: Series, fx: Series | None, day: date) -> float | None:
    """Close as-of ``day`` converted to CAD; None if unlisted or stale (> _MAX_STALE_DAYS)."""
    i = series.idx_asof(day)
    if i < 0 or series.dates[i] < day - timedelta(days=_MAX_STALE_DAYS):
        return None
    return series.closes[i] * _fx_to_cad(series.currency, fx, day)


def _rank_labels(df: pd.DataFrame) -> pd.DataFrame:
    """Percentile-rank ``fwd_excess_return`` within each date into ``label`` ∈ [0, 1].

    Average-ranked then rescaled so the best name on a date is 1.0 and the worst 0.0
    (a single-name date degenerates to 0.5 — no cross-section to rank against).
    """
    ranks = df.groupby("date")["fwd_excess_return"].rank(method="average")
    counts = df.groupby("date")["fwd_excess_return"].transform("count")
    df["label"] = ((ranks - 1.0) / (counts - 1.0)).where(counts > 1, 0.5)
    return df


def build_dataset_from_panel(
    universe: dict[int, Series],
    benchmark: Series,
    *,
    fx: Series | None = None,
    corporate: dict[int, CorporateData] | None = None,
    start: date,
    end: date,
    limits: EligibilityLimits | None = None,
    horizon_months: int = LABEL_HORIZON_MONTHS,
) -> pd.DataFrame:
    """Pure dataset core over in-memory panels — no DB, no network.

    No-look-ahead contract: a row at rebalance date t reads bars/corporate data ≤ t for its
    features and eligibility, and bars in (t, t+horizon] only for its label. Rows lacking a
    full forward window are excluded, so the dataset necessarily ends ``horizon_months``
    before the last bar. Returns a frame with META_COLUMNS plus one column per feature
    (NaN where an instrument lacks that feature) and the categorical ``sector``.
    """
    config = RegisteredStrategyConfig()
    calendar = sorted(
        {day for series in universe.values() for day in series.dates if start <= day <= end}
    )
    rebalance_dates = month_end_rebalance_dates(calendar)

    rows: list[dict[str, object]] = []
    for i, day in enumerate(rebalance_dates):
        fwd_i = i + horizon_months
        if fwd_i >= len(rebalance_dates):
            break  # no full forward window left
        fwd_day = rebalance_dates[fwd_i]
        # guard against ingestion holes: index arithmetic must equal the calendar horizon,
        # otherwise the "12-month" label would silently span more months
        months_apart = (fwd_day.year * 12 + fwd_day.month) - (day.year * 12 + day.month)
        if months_apart != horizon_months:
            continue

        bench_now = _cad_close_asof(benchmark, fx, day)
        bench_fwd = _cad_close_asof(benchmark, fx, fwd_day)
        if bench_now is None or bench_fwd is None or bench_now <= 0:
            continue  # no benchmark → no excess return for this date
        bench_return = bench_fwd / bench_now - 1.0

        result = screen(_build_candidates(universe, fx, day, config), limits)
        features = _features_asof(
            universe, result.eligible_ids, day, benchmark=benchmark, corporate=corporate
        )
        for iid in sorted(result.eligible_ids):
            series = universe[iid]
            close_now = _cad_close_asof(series, fx, day)
            close_fwd = _cad_close_asof(series, fx, fwd_day)
            if close_now is None or close_fwd is None or close_now <= 0:
                continue
            corp = (corporate or {}).get(iid)
            row: dict[str, object] = {
                "instrument_id": iid,
                "symbol": series.symbol,
                "date": day,
                "sector": corp.sector if corp is not None else None,
                "fwd_excess_return": close_fwd / close_now - 1.0 - bench_return,
            }
            row.update(features.get(iid, {}))
            rows.append(row)

    if not rows:
        return pd.DataFrame(columns=list(META_COLUMNS) + ["sector"])
    df = pd.DataFrame(rows)
    df = _rank_labels(df)
    ordered = [c for c in META_COLUMNS if c in df.columns] + sorted(feature_columns(df))
    return df[ordered].reset_index(drop=True)


def build_dataset(
    session: Session,
    universe: Sequence[str],
    start: datetime,
    end: datetime,
    *,
    benchmark_symbol: str = "XEQT",
    limits: EligibilityLimits | None = None,
    horizon_months: int = LABEL_HORIZON_MONTHS,
) -> pd.DataFrame:
    """Load bars/FX/corporate data from Postgres and build the supervised dataset.

    ``universe`` is symbols (one per tickers.txt line). Bars load over [start, end]; labeled
    rows end ``horizon_months`` before the last bar in the window. Raises ValueError when the
    universe or the benchmark has no bars — an excess-return label can't exist without XEQT.
    """
    panel = _load_universe(session, list(universe), start, end)
    if not panel:
        raise ValueError(f"no daily bars found for {list(universe)!r} in the requested window")
    benchmark = _load_series(session, benchmark_symbol, start, end)
    if benchmark is None:
        raise ValueError(
            f"benchmark {benchmark_symbol!r} has no bars — the label is excess-vs-benchmark"
        )
    fx = _load_series(session, "USDCAD", start, end)
    corporate = {iid: load_corporate_inputs(session, iid) for iid in panel}
    return build_dataset_from_panel(
        panel,
        benchmark,
        fx=fx,
        corporate=corporate,
        start=start.astimezone(UTC).date() if start.tzinfo else start.date(),
        end=end.astimezone(UTC).date() if end.tzinfo else end.date(),
        limits=limits,
        horizon_months=horizon_months,
    )
