"""Point-in-time index universes — "what was investable *then*", not today's survivors.

A curated ticker file is a list of companies that survived to today, which flatters every
stock-picking backtest. The fix is to ask, on each decision date, which companies were in
the index on that date — including the ones that later went bankrupt or were bought — and
to price them. Membership spans and their pricing come from the data-lake
(``index_memberships``, filled by ``ingestion.market.index_membership`` and priced by
``ingestion.market.index_pricing``); this module turns them into simulator ``Series`` whose
``member_ranges`` gate them, day by day, in ``engine._build_candidates``.

Free sources do not price every dead member (renamed tickers, reused tickers, pre-2016
delistings are thin). That residue is a bias of its own, so it is **measured, never hidden**:
``coverage`` reports what share of the index's members were priced on sampled days, and the
lab prints it beside every result.

An index admits a company only after it has seasoned, so the **IPO cohort**
(``with_ipo_cohort``) adds every listing the data-lake priced from ``ipo_events``, dead ones
included, as an ungated candidate from its first bar — the young names an index never shows.

Pure functions over spans and ``Series``; ``load_index_universe`` is the thin DB wrapper,
and ``UniverseSpec``/``load_universe_panel`` the entry point the lab's siblings — the
training dataset and the OOS backtest — use, so a model is trained and judged on the same
point-in-time universe the strategies are.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ibkr_trader.backtest.engine import (
    LoadedUniverse,
    Series,
    _load_series,
    _load_universe,
    _universe_hash,
    curated_provenance,
    load_instrument_series,
)
from ibkr_trader.db.models import IndexMembership, Instrument, IpoEvent

#: Index code the S&P 500 spans are stored under.
SP500 = "SP500"


@dataclass(frozen=True)
class Span:
    """One membership span as the universe sees it (a row of ``index_memberships``)."""

    symbol: str
    start: date
    end: date | None
    instrument_id: int | None = None
    resolution: str | None = None

    def contains(self, day: date) -> bool:
        return self.start <= day and (self.end is None or day <= self.end)

    def overlaps(self, start: date, end: date) -> bool:
        return self.start <= end and (self.end is None or self.end >= start)


def member_ranges(spans: Iterable[Span]) -> dict[int, tuple[tuple[date, date | None], ...]]:
    """Priced spans grouped by the instrument that prices them, oldest first.

    One instrument can carry several spans (dropped from the index and re-added later), and
    the engine treats it as a member inside any of them. Unpriced spans are left out.
    """
    grouped: dict[int, list[tuple[date, date | None]]] = defaultdict(list)
    for span in spans:
        if span.instrument_id is not None:
            grouped[span.instrument_id].append((span.start, span.end))
    return {iid: tuple(sorted(ranges)) for iid, ranges in grouped.items()}


@dataclass
class Coverage:
    """How much of the index a universe could actually price, sampled over its window."""

    #: (day, index members that day, of which priced and trading)
    samples: list[tuple[date, int, int]] = field(default_factory=list)
    #: spans inside the window that no free source could price (the residual bias)
    unpriced: list[Span] = field(default_factory=list)
    #: spans inside the window the backfill has not reached yet (a gap that will close)
    pending: list[Span] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        """Priced member-days / member-days over the samples (1.0 when there are none)."""
        members = sum(n for _, n, _ in self.samples)
        return sum(p for _, _, p in self.samples) / members if members else 1.0

    def ratio_since(self, day: date) -> float:
        """``ratio`` restricted to samples on or after ``day`` — coverage of a recent window."""
        recent = Coverage([s for s in self.samples if s[0] >= day])
        return recent.ratio


def coverage(
    spans: Iterable[Span],
    universe: dict[int, Series],
    start: date,
    end: date,
    *,
    step_days: int = 30,
) -> Coverage:
    """Sample every ``step_days`` from ``start`` to ``end``: members vs priced members.

    A member counts as priced on a day only if its instrument was loaded *and* had traded by
    then and not yet stopped — "the resolver said so" is not enough; the bars must be there.
    """
    spans = list(spans)
    missing = [s for s in spans if s.instrument_id is None and s.overlaps(start, end)]
    result = Coverage(
        unpriced=[s for s in missing if s.resolution is not None],
        pending=[s for s in missing if s.resolution is None],
    )
    day = start
    while day <= end:
        members = [s for s in spans if s.contains(day)]
        priced = sum(1 for s in members if _trading(universe.get(s.instrument_id or -1), day))
        result.samples.append((day, len(members), priced))
        day += timedelta(days=step_days)
    return result


def _trading(series: Series | None, day: date) -> bool:
    return series is not None and series.idx_asof(day) >= 0 and not series.has_ended(day)


def load_spans(session: Session, index_code: str, start: date, end: date) -> list[Span]:
    """Every membership span of ``index_code`` that overlaps ``[start, end]``."""
    rows = session.scalars(
        select(IndexMembership).where(
            IndexMembership.index_code == index_code,
            IndexMembership.start_date <= end,
            or_(IndexMembership.end_date.is_(None), IndexMembership.end_date >= start),
        )
    ).all()
    return [
        Span(row.symbol, row.start_date, row.end_date, row.instrument_id, row.resolution)
        for row in rows
    ]


def load_index_universe(
    session: Session,
    index_code: str,
    window: tuple[datetime, datetime],
    extra_symbols: Iterable[str] = (),
    *,
    validate: bool = True,
) -> tuple[dict[int, Series], list[Span]]:
    """Bars for every instrument that priced a member of ``index_code`` inside ``window``,
    gated by ``member_ranges``, plus ``extra_symbols`` (e.g. the broad ETFs a couch-potato
    reference holds) loaded ungated. Returns the universe and the spans, for ``coverage``.

    ``window`` is the full bar window, warm-up included: a member's bars from before it
    joined the index still feed its momentum features on the day it joins.
    """
    start, end = window
    spans = load_spans(session, index_code, start.date(), end.date())
    universe: dict[int, Series] = {}
    for iid, member in member_ranges(spans).items():
        instrument = session.get(Instrument, iid)
        series = (
            load_instrument_series(session, instrument, start, end, validate=validate)
            if instrument
            else None
        )
        if series is not None:
            universe[iid] = replace(series, member_ranges=member)
    for symbol in extra_symbols:
        extra = _load_series(session, symbol, start, end, validate=validate)
        if extra is not None and extra.instrument_id not in universe:
            universe[extra.instrument_id] = extra
    return universe, spans


def index_provenance(
    index_code: str,
    extra_symbols: Iterable[str],
    universe: dict[int, Series],
    measured: Coverage,
) -> dict:
    """Provenance stamp of a point-in-time index universe, coverage residue included.

    Keeps the curated stamp's ``n_symbols``/``sha256_16`` keys so every reader of a run's or a
    model's ``universe`` works unchanged; the hash covers the instruments actually loaded.
    """
    return {
        "source": f"index-point-in-time:{index_code}",
        "index": index_code,
        "extra_symbols": sorted({symbol.upper() for symbol in extra_symbols}),
        "n_symbols": len(universe),
        "sha256_16": _universe_hash([f"{iid}:{s.symbol}" for iid, s in universe.items()]),
        "survivorship": "point-in-time",
        "coverage_ratio": round(measured.ratio, 4),
        "unpriced_spans": len(measured.unpriced),
        "pending_spans": len(measured.pending),
    }


def load_universe_panel(
    session: Session,
    symbols: Sequence[str],
    window: tuple[datetime, datetime],
    *,
    index_code: str | None = None,
    measure_from: date | None = None,
    validate: bool = True,
) -> LoadedUniverse:
    """The panel a model is trained or simulated on, with its provenance stamped.

    ``index_code`` None → ``symbols`` exactly as given: a curated list of today's survivors,
    stamped ``curated-current``. Set → that index as it stood on each day (later-delisted
    members included) plus ``symbols`` loaded ungated — pass ETFs there, never today's
    single names, or the survivors come back in through the side door. Coverage is measured
    from ``measure_from`` (default: the window start) to the window end.
    """
    if index_code is None:
        return LoadedUniverse(
            _load_universe(session, list(symbols), *window, validate=validate),
            curated_provenance(list(symbols)),
        )
    universe, spans = load_index_universe(session, index_code, window, symbols, validate=validate)
    measured = coverage(spans, universe, measure_from or window[0].date(), window[1].date())
    return LoadedUniverse(universe, index_provenance(index_code, symbols, universe, measured))


def load_ipo_cohort(
    session: Session, window: tuple[datetime, datetime], *, validate: bool = True
) -> tuple[dict[int, Series], dict[str, int]]:
    """Every instrument an IPO listing was priced with that has bars in ``window``, ungated,
    plus the residue: listings dated inside the window that are not in it.

    The cohort is the IPO calendar's priced deals (``ipo_events`` from ``finnhub`` at stage
    ``priced``, the rows the data-lake's IPO pricing resolves) that found an instrument —
    later delistings included, so it is chosen by an event known on the listing day, not by
    who survived. The residue is measured like ``coverage``: ``unpriced_listings`` no free
    source could price, and ``pending_listings`` not attempted yet (mostly dead names waiting
    for Tiingo budget — the part that leaves the cohort tilted towards survivors until it is
    priced). ``excluded:<reason>`` rows (units, warrants, SPAC shells) are neither.
    """
    start, end = window
    rows = session.execute(
        select(IpoEvent.instrument_id, IpoEvent.resolution, IpoEvent.expected_date).where(
            IpoEvent.source == "finnhub", IpoEvent.stage == "priced"
        )
    ).all()
    residue = {"unpriced_listings": 0, "pending_listings": 0}
    residue_key = {None: "pending_listings", "unpriced": "unpriced_listings"}
    priced: set[int] = set()
    for iid, resolution, day in rows:
        if iid is not None:
            priced.add(iid)
        elif resolution in residue_key and day and start.date() <= day <= end.date():
            residue[residue_key[resolution]] += 1
    cohort: dict[int, Series] = {}
    for iid in sorted(priced):
        instrument = session.get(Instrument, iid)
        series = (
            load_instrument_series(session, instrument, start, end, validate=validate)
            if instrument
            else None
        )
        if series is not None:
            cohort[iid] = series
    return cohort, residue


def with_ipo_cohort(
    session: Session,
    loaded: LoadedUniverse,
    window: tuple[datetime, datetime],
    *,
    validate: bool = True,
) -> LoadedUniverse:
    """``loaded`` plus the IPO cohort, each cohort name a candidate for life from its listing.

    A cohort name the index also gates is ungated: it is in the universe for having listed,
    and the index joining it later must not hide its first months. The provenance keeps the
    base's survivorship verdict — a cohort added to a curated list is still survivor-biased.
    """
    cohort, residue = load_ipo_cohort(session, window, validate=validate)
    series = dict(loaded.series)
    for iid, listed in cohort.items():
        series[iid] = replace(series.get(iid, listed), member_ranges=None)
    provenance = {
        **loaded.provenance,
        "source": f"{loaded.provenance['source']}+ipo-cohort",
        "n_symbols": len(series),
        "sha256_16": _universe_hash([f"{iid}:{s.symbol}" for iid, s in series.items()]),
        "ipo_cohort": {"instruments": len(cohort), **residue},
    }
    return LoadedUniverse(series, provenance)


@dataclass(frozen=True)
class UniverseSpec:
    """What a model is trained and judged on, before any bar is loaded.

    The training dataset and the OOS simulation load the same spec over different bar
    windows; ``signals.dataset`` takes it through a structural protocol rather than importing
    this module, which would pull it into the signals↔engine import cycle.
    """

    symbols: tuple[str, ...]
    #: None → ``symbols`` is the whole (curated) universe; else that index point-in-time.
    index_code: str | None = None
    #: Add every priced IPO listing (``with_ipo_cohort``) — the fresh listings an index only
    #: admits after a year of seasoning, if ever.
    ipo_cohort: bool = False

    def load(
        self,
        session: Session,
        window: tuple[datetime, datetime],
        *,
        measure_from: date | None = None,
        validate: bool = True,
    ) -> LoadedUniverse:
        """``load_universe_panel`` for this spec, plus the IPO cohort when it asks for one."""
        loaded = load_universe_panel(
            session,
            self.symbols,
            window,
            index_code=self.index_code,
            measure_from=measure_from,
            validate=validate,
        )
        if not self.ipo_cohort:
            return loaded
        return with_ipo_cohort(session, loaded, window, validate=validate)
