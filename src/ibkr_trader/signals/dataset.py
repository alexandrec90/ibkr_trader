"""Supervised dataset for the long-term model (ML-03).

One row per (instrument, monthly rebalance date t): current feature set as-of t, the eligibility
screen as-of t, and the label fixed by prior decision — **12-month forward total return in CAD
(ADJUSTED_LAST bars, dividends reinvested), in excess of XEQT, percentile-ranked
cross-sectionally per rebalance date** into [0, 1].

Layout mirrors signals.features: a pure core (``build_dataset_from_panel``) over in-memory
``Series`` panels so the no-look-ahead and rank-label properties are unit-testable without a
database, plus a thin Postgres wrapper (``build_dataset``) that loads bars through the engine's
one-source-per-instrument loader. No network anywhere.

Incomplete histories are kept, never silently dropped — dropping them is survivorship bias:

- **Right-censored labels.** A name whose bars stop inside its forward window (delisted,
  acquired, bankrupt) is labeled with its return to its last close, held as cash after —
  what the simulator's ``_settle_delisted`` pays — and flagged ``label_censored``.
- **Young listings.** The listing-history floor is a holding rule, not a training filter
  (``TRAINING_LIMITS``): a fresh IPO or spinoff is a row from its first month, with
  ``history_days`` telling the model how young it is and too-long windows left NaN. The
  listing is the stored ``listed_on`` where the bars agree with it, else the first bar.
- **Point-in-time universe.** ``build_dataset`` takes a ``UniverseSource`` (the S&P 500
  spec in ``backtest.universe``) so the rows are the index as it stood each day, later
  failures included.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol, runtime_checkable

import pandas as pd
from sqlalchemy.orm import Session

from ibkr_trader.backtest.engine import (
    LoadedUniverse,
    RegisteredStrategyConfig,
    Series,
    _build_candidates,
    _features_asof,
    _fx_to_cad,
    _load_series,
    _load_universe,
    curated_provenance,
)
from ibkr_trader.signals.eligibility import EligibilityLimits, screen
from ibkr_trader.signals.features import (
    _HISTORY_BUFFER_DAYS,
    TRADING_DAYS_PER_YEAR,
    CorporateData,
    load_corporate_inputs,
)
from ibkr_trader.signals.schemas import (
    NUMERIC_FEATURE_COLUMNS,
    validate_feature_frame,
)

#: The label's forward horizon, in monthly rebalance periods. Walk-forward validation must
#: purge at least this many months between a train window's end and its test window's start.
LABEL_HORIZON_MONTHS = 12

#: Non-feature columns. ``fwd_excess_return`` is the raw (un-ranked) excess return kept for
#: diagnostics — it is *forward-looking* and must never be fed to a model as a feature, and
#: neither may ``label_censored`` (whether the name dies within the horizon); anything not
#: listed here is a feature (numeric, plus the categorical ``sector``).
META_COLUMNS = ("instrument_id", "symbol", "date", "fwd_excess_return", "label", "label_censored")

#: The training screen: the default price, liquidity, currency and leverage floors, but no
#: listing-history floor. How much history a name needs before it may be *held* is the
#: simulator's ``EligibilityLimits``; what young names go on to do is what a model must learn.
TRAINING_LIMITS = EligibilityLimits(min_history_days=1)

#: A label endpoint's as-of bar must be at most this many calendar days stale; older means an
#: ingestion hole the series later resumes from, whose outcome is unknown (a stop for good is
#: a censored label instead — see ``_label_endpoint``).
_MAX_STALE_DAYS = 20

#: How far a first bar may trail the start it is judged against and still be that start. A
#: series is a fresh listing when its first bar lies this close to its stored ``listed_on``;
#: without one, when its first bar lies more than this past the panel's earliest bar — one
#: starting with the *loaded data* says nothing about its age. A series whose age is not
#: known that way waits for a full year of bars rather than posing as a fresh IPO.
_LISTING_SLACK_DAYS = 31


@dataclass(frozen=True)
class DatasetBuild:
    """The labeled frame plus the provenance of the universe it was built on."""

    frame: pd.DataFrame
    #: ``LoadedUniverse.provenance`` — survivorship status, size, hash, coverage.
    universe: dict


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


def _label_endpoint(
    series: Series, fx: Series | None, day: date, fwd_day: date
) -> tuple[float, bool] | None:
    """CAD value at the label's end, and whether it is right-censored; None if unknown.

    A name whose bars stopped for good before ``fwd_day`` (``Series.has_ended``, judged over
    the whole loaded series exactly as the simulator judges it) is cashed out at its last
    close and held as cash to the horizon: the censored outcome, not a missing one. A stale
    endpoint on a series that resumes later is an ingestion hole — unknown, so no row.
    """
    if series.has_ended(fwd_day):
        settled = max(series.dates[-1], day)
        return series.closes[-1] * _fx_to_cad(series.currency, fx, settled), True
    close = _cad_close_asof(series, fx, fwd_day)
    return None if close is None else (close, False)


def _age_unknown(universe: dict[int, Series]) -> set[int]:
    """Instruments whose first loaded bar is not (knowably) their listing.

    A stored ``listed_on`` decides when the bars agree with it: a first bar near it is the
    listing, one well after it means the bars begin late. One the bars contradict (they
    start before it) is ignored, and the first bar is judged against the data's start.
    """
    firsts = {iid: series.dates[0] for iid, series in universe.items() if series.dates}
    if not firsts:
        return set()
    slack = timedelta(days=_LISTING_SLACK_DAYS)
    data_start_cutoff = min(firsts.values()) + slack
    unknown: set[int] = set()
    for iid, first in firsts.items():
        listed_on = universe[iid].listed_on
        if listed_on is not None and listed_on <= first + slack:
            begins_late = first > listed_on + slack
        else:
            begins_late = first <= data_start_cutoff
        if begins_late:
            unknown.add(iid)
    return unknown


def _rank_labels(df: pd.DataFrame) -> pd.DataFrame:
    """Percentile-rank ``fwd_excess_return`` within each date into ``label`` ∈ [0, 1].

    Average-ranked then rescaled so the best name on a date is 1.0 and the worst 0.0
    (a single-name date degenerates to 0.5 — no cross-section to rank against).
    """
    ranks = df.groupby("date")["fwd_excess_return"].rank(method="average")
    counts = df.groupby("date")["fwd_excess_return"].transform("count")
    df["label"] = ((ranks - 1.0) / (counts - 1.0)).where(counts > 1, 0.5)
    return df


@dataclass(frozen=True)
class _Panel:
    """Everything one rebalance date's rows read — the dataset core's in-memory inputs."""

    universe: dict[int, Series]
    benchmark: Series
    fx: Series | None
    corporate: dict[int, CorporateData]
    age_unknown: set[int]


def _own_return(panel: _Panel, iid: int, day: date, fwd_day: date) -> tuple[float, bool] | None:
    """CAD return from ``day`` to the label's end and whether it is censored; None → no row."""
    series = panel.universe[iid]
    if iid in panel.age_unknown and series.idx_asof(day) + 1 < TRADING_DAYS_PER_YEAR:
        return None  # the data window's start must not pose as a fresh listing
    close_now = _cad_close_asof(series, panel.fx, day)
    endpoint = _label_endpoint(series, panel.fx, day, fwd_day)
    if close_now is None or endpoint is None or close_now <= 0:
        return None
    close_fwd, censored = endpoint
    return close_fwd / close_now - 1.0, censored


def _rows_for_date(
    panel: _Panel, day: date, fwd_day: date, limits: EligibilityLimits
) -> list[dict[str, object]]:
    """One labeled row per eligible name at ``day`` (features ≤ day, label to ``fwd_day``)."""
    bench_now = _cad_close_asof(panel.benchmark, panel.fx, day)
    bench_fwd = _cad_close_asof(panel.benchmark, panel.fx, fwd_day)
    if bench_now is None or bench_fwd is None or bench_now <= 0:
        return []  # no benchmark → no excess return for this date
    bench_return = bench_fwd / bench_now - 1.0

    candidates = _build_candidates(panel.universe, panel.fx, day, RegisteredStrategyConfig())
    result = screen(candidates, limits)
    features = _features_asof(
        panel.universe,
        result.eligible_ids,
        day,
        benchmark=panel.benchmark,
        corporate=panel.corporate,
    )
    rows: list[dict[str, object]] = []
    for iid in sorted(result.eligible_ids):
        outcome = _own_return(panel, iid, day, fwd_day)
        if outcome is None:
            continue
        corp = panel.corporate.get(iid)
        rows.append(
            {
                "instrument_id": iid,
                "symbol": panel.universe[iid].symbol,
                "date": day,
                "sector": corp.sector if corp is not None else None,
                "fwd_excess_return": outcome[0] - bench_return,
                "label_censored": outcome[1],
                **features.get(iid, {}),
            }
        )
    return rows


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "instrument_id": pd.Series(dtype="int64"),
            "symbol": pd.Series(dtype="object"),
            "date": pd.Series(dtype="object"),
            "fwd_excess_return": pd.Series(dtype="float64"),
            "label": pd.Series(dtype="float64"),
            "label_censored": pd.Series(dtype="bool"),
            "sector": pd.Series(dtype="object"),
        }
    )


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
    validate: bool = True,
) -> pd.DataFrame:
    """Pure dataset core over in-memory panels — no DB, no network.

    No-look-ahead contract: a row at rebalance date t reads bars/corporate data ≤ t for its
    features and eligibility, and bars in (t, t+horizon] only for its label (whether a
    stopped series ever resumes is judged over the whole panel, as the simulator does).
    Rows lacking a full forward window are excluded, so the dataset necessarily ends
    ``horizon_months`` before the last bar. ``limits`` defaults to ``TRAINING_LIMITS``.
    Returns a frame with META_COLUMNS plus one column per feature (NaN where an instrument
    lacks that feature) and the categorical ``sector``.
    """
    panel = _Panel(universe, benchmark, fx, corporate or {}, _age_unknown(universe))
    calendar = sorted(
        {day for series in universe.values() for day in series.dates if start <= day <= end}
    )
    rebalance_dates = month_end_rebalance_dates(calendar)

    rows: list[dict[str, object]] = []
    for day, fwd_day in zip(rebalance_dates, rebalance_dates[horizon_months:], strict=False):
        # guard against ingestion holes: index arithmetic must equal the calendar horizon,
        # otherwise the "12-month" label would silently span more months
        months_apart = (fwd_day.year * 12 + fwd_day.month) - (day.year * 12 + day.month)
        if months_apart == horizon_months:
            rows.extend(_rows_for_date(panel, day, fwd_day, limits or TRAINING_LIMITS))

    df = _rank_labels(pd.DataFrame(rows)) if rows else _empty_frame()
    # A versioned training frame has a stable column contract. Missing observations stay NaN;
    # a feature missing for the entire requested window must not silently remove its column.
    for column in NUMERIC_FEATURE_COLUMNS:
        if column not in df:
            df[column] = pd.Series(index=df.index, dtype="float64")
    if "sector" not in df:
        df["sector"] = pd.Series(index=df.index, dtype="object")
    ordered = [c for c in META_COLUMNS if c in df.columns] + sorted(feature_columns(df))
    df = df[ordered].reset_index(drop=True)
    return validate_feature_frame(df) if validate else df


@runtime_checkable
class UniverseSource(Protocol):
    """Loads a universe panel over a bar window — ``backtest.universe.UniverseSpec``.

    A protocol rather than an import: this module sits inside the signals↔engine import
    cycle, and naming ``backtest.universe`` here would pull that module into it too.
    """

    def load(
        self,
        session: Session,
        window: tuple[datetime, datetime],
        *,
        measure_from: date | None = None,
        validate: bool = True,
    ) -> LoadedUniverse: ...


def dataset_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """The bar window behind a dataset: ``_HISTORY_BUFFER_DAYS`` of warm-up before ``start``,
    so names alive at ``start`` have full lookbacks from its first rebalance date."""
    return start - timedelta(days=_HISTORY_BUFFER_DAYS), end


def build_dataset(
    session: Session,
    universe: Sequence[str] | UniverseSource,
    start: datetime,
    end: datetime,
    *,
    benchmark_symbol: str = "XEQT",
    limits: EligibilityLimits | None = None,
    horizon_months: int = LABEL_HORIZON_MONTHS,
    validate: bool = True,
) -> DatasetBuild:
    """Load bars/FX/corporate data from Postgres and build the supervised dataset.

    ``universe`` is a symbol list (curated, survivor-biased) or a ``UniverseSource`` such
    as the point-in-time S&P 500 spec. Bars load over ``dataset_window``; labeled rows end
    ``horizon_months`` before the last bar. Raises ValueError when the universe or the
    benchmark has no bars — an excess-return label can't exist without XEQT.
    """
    window = dataset_window(start, end)
    if isinstance(universe, UniverseSource):
        loaded = universe.load(session, window, measure_from=_utc_date(start), validate=validate)
    else:
        symbols = list(universe)
        series = _load_universe(session, symbols, *window, validate=validate)
        loaded = LoadedUniverse(series, curated_provenance(symbols))
    panel = loaded.series
    if not panel:
        raise ValueError(f"no daily bars found for {universe!r} in the requested window")
    benchmark = _load_series(session, benchmark_symbol, *window, validate=validate)
    if benchmark is None:
        raise ValueError(
            f"benchmark {benchmark_symbol!r} has no bars — the label is excess-vs-benchmark"
        )
    fx = _load_series(session, "USDCAD", *window, validate=validate)
    corporate = {iid: load_corporate_inputs(session, iid) for iid in panel}
    frame = build_dataset_from_panel(
        panel,
        benchmark,
        fx=fx,
        corporate=corporate,
        start=_utc_date(start),
        end=_utc_date(end),
        limits=limits,
        horizon_months=horizon_months,
        validate=validate,
    )
    return DatasetBuild(frame, loaded.provenance)


def _utc_date(moment: datetime) -> date:
    return moment.astimezone(UTC).date() if moment.tzinfo else moment.date()
