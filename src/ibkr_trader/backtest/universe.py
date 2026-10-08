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

Pure functions over spans and ``Series``; ``load_index_universe`` is the thin DB wrapper.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ibkr_trader.backtest.engine import Series, _load_series, load_instrument_series
from ibkr_trader.db.models import IndexMembership, Instrument

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
        series = load_instrument_series(session, instrument, start, end) if instrument else None
        if series is not None:
            universe[iid] = replace(series, member_ranges=member)
    for symbol in extra_symbols:
        extra = _load_series(session, symbol, start, end)
        if extra is not None and extra.instrument_id not in universe:
            universe[extra.instrument_id] = extra
    return universe, spans
