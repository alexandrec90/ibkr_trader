"""One dated list of market events, for `ibkr-trader events`.

The lake keeps each kind of event in its own table, because each has its own shape and its
own source. This module reads them back into one list for a date window:

- ``earnings_events`` -- report dates, with EPS estimate/actual when the source had them.
  Yahoo and Finnhub both report most US names, so rows for one symbol on one day merge.
- ``corporate_events`` -- announced next ex-dividend and dividend-payment dates.
- ``stock_splits`` -- split history (a past window shows it; splits are not announced ahead
  by any free source the lake reads).
- ``economic_events`` -- FOMC meetings, Bank of Canada rate announcements, FRED data releases
  (CPI, jobs, GDP, ...).
- ``ipo_events`` -- calendar deals with an expected date.

Read-only, DB only: nothing here calls a provider.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ibkr_trader.db.models import (
    CorporateEvent,
    EarningsEvent,
    EconomicEvent,
    Instrument,
    IpoEvent,
    StockSplit,
)

#: Every kind the list can hold, in the order same-day events print.
KINDS: tuple[str, ...] = (
    "fomc_meeting",
    "boc_rate_announcement",
    "data_release",
    "earnings",
    "ex_dividend",
    "dividend_payment",
    "split",
    "ipo",
)

_CORPORATE_WORDS = {"ex_dividend": "ex-dividend", "dividend_payment": "dividend paid"}
_HOUR_WORDS = {"bmo": "before open", "amc": "after close", "dmh": "during market hours"}


@dataclass(frozen=True)
class EventRow:
    """One event: its day, kind (one of ``KINDS``), what it is about, and a short detail."""

    day: date
    kind: str
    subject: str  # a ticker, a country code, or a company name
    detail: str


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """[start 00:00 UTC, the day after end 00:00 UTC): ``end`` is inclusive."""
    return (
        datetime.combine(start, time.min, tzinfo=UTC),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
    )


def _as_utc_date(moment: datetime) -> date:
    """The UTC calendar day of a stored timestamp (SQLite returns them naive)."""
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).date()


def earnings_detail(
    hour: str | None,
    eps_estimate: float | None,
    eps_actual: float | None,
    surprise_pct: float | None,
) -> str:
    """'after close, EPS 1.30 vs est 1.23 (+5.7%)' -- each part only when known."""
    parts: list[str] = []
    if hour and hour in _HOUR_WORDS:
        parts.append(_HOUR_WORDS[hour])
    if eps_actual is not None:
        eps = f"EPS {eps_actual:.2f}"
        if eps_estimate is not None:
            eps += f" vs est {eps_estimate:.2f}"
        if surprise_pct is not None:
            eps += f" ({surprise_pct:+.1f}%)"
        parts.append(eps)
    elif eps_estimate is not None:
        parts.append(f"EPS est {eps_estimate:.2f}")
    return ", ".join(parts) or "earnings report"


def split_detail(ratio: float) -> str:
    """4.0 -> '4-for-1 split'; 0.1 -> '1-for-10 reverse split'."""
    if ratio >= 1:
        return f"{ratio:g}-for-1 split"
    return f"1-for-{1 / ratio:g} reverse split"


def economic_detail(kind: str, name: str, raw: dict | None) -> str:
    """The event's name, with the flags worth a glance: SEP and notation vote, MPR."""
    flags = raw or {}
    notes: list[str] = []
    if kind == "fomc_meeting":
        if flags.get("sep"):
            notes.append("with projections")
        if flags.get("notation_vote"):
            notes.append("notation vote")
    elif kind == "boc_rate_announcement" and flags.get("mpr"):
        notes.append("with Monetary Policy Report")
    return f"{name} ({', '.join(notes)})" if notes else name


def _earnings(
    session: Session, start: date, end: date, symbols: Collection[str] | None
) -> list[EventRow]:
    low, high = _bounds(start, end)
    query = (
        select(EarningsEvent, Instrument.symbol)
        .join(Instrument, Instrument.id == EarningsEvent.instrument_id)
        .where(EarningsEvent.report_ts >= low, EarningsEvent.report_ts < high)
        .order_by(EarningsEvent.report_ts, EarningsEvent.source)
    )
    if symbols is not None:
        query = query.where(Instrument.symbol.in_(list(symbols)))
    # Yahoo and Finnhub both list most US names: one event per (symbol, day), each field
    # taken from the first source that has it.
    merged: dict[tuple[str, date], list[EarningsEvent]] = {}
    for event, symbol in session.execute(query):
        merged.setdefault((symbol, _as_utc_date(event.report_ts)), []).append(event)

    def first(events: list[EarningsEvent], name: str):
        return next((getattr(e, name) for e in events if getattr(e, name) is not None), None)

    return [
        EventRow(
            day,
            "earnings",
            symbol,
            earnings_detail(
                first(events, "hour"),
                first(events, "eps_estimate"),
                first(events, "eps_actual"),
                first(events, "surprise_pct"),
            ),
        )
        for (symbol, day), events in merged.items()
    ]


def _corporate(
    session: Session, start: date, end: date, symbols: Collection[str] | None
) -> list[EventRow]:
    query = (
        select(CorporateEvent.event_date, CorporateEvent.kind, Instrument.symbol)
        .join(Instrument, Instrument.id == CorporateEvent.instrument_id)
        .where(CorporateEvent.event_date >= start, CorporateEvent.event_date <= end)
        .distinct()
    )
    if symbols is not None:
        query = query.where(Instrument.symbol.in_(list(symbols)))
    return [
        EventRow(day, kind, symbol, _CORPORATE_WORDS.get(kind, kind))
        for day, kind, symbol in session.execute(query)
    ]


def _splits(
    session: Session, start: date, end: date, symbols: Collection[str] | None
) -> list[EventRow]:
    query = (
        select(StockSplit.ex_date, StockSplit.ratio, Instrument.symbol)
        .join(Instrument, Instrument.id == StockSplit.instrument_id)
        .where(StockSplit.ex_date >= start, StockSplit.ex_date <= end)
        .distinct()
    )
    if symbols is not None:
        query = query.where(Instrument.symbol.in_(list(symbols)))
    return [
        EventRow(day, "split", symbol, split_detail(ratio))
        for day, ratio, symbol in session.execute(query)
    ]


def _economic(session: Session, start: date, end: date) -> list[EventRow]:
    rows = session.scalars(
        select(EconomicEvent).where(
            EconomicEvent.event_date >= start, EconomicEvent.event_date <= end
        )
    )
    return [
        EventRow(
            row.event_date, row.kind, row.country, economic_detail(row.kind, row.name, row.raw)
        )
        for row in rows
    ]


def _ipos(session: Session, start: date, end: date) -> list[EventRow]:
    rows = session.scalars(
        select(IpoEvent).where(IpoEvent.expected_date >= start, IpoEvent.expected_date <= end)
    )
    events: list[EventRow] = []
    for row in rows:
        assert row.expected_date is not None  # the query bounds it
        listing = f" as {row.symbol}" if row.symbol else ""
        spac = " (SPAC)" if row.is_spac else ""
        events.append(
            EventRow(row.expected_date, "ipo", row.company_name, f"IPO {row.stage}{listing}{spac}")
        )
    return events


def load_events(
    session: Session,
    start: date,
    end: date,
    *,
    symbols: Collection[str] | None = None,
    kinds: Collection[str] | None = None,
) -> list[EventRow]:
    """Every event dated ``start`` through ``end`` (inclusive), in print order.

    ``symbols`` narrows the per-instrument kinds (earnings, dividends, splits); the macro
    calendar and IPOs are not about a held symbol and are kept. ``kinds`` keeps only those.
    """
    wanted = set(kinds) if kinds is not None else set(KINDS)
    unknown = wanted - set(KINDS)
    if unknown:
        raise ValueError(f"unknown event kind(s): {', '.join(sorted(unknown))}")
    rows: list[EventRow] = []
    if "earnings" in wanted:
        rows += _earnings(session, start, end, symbols)
    if wanted & {"ex_dividend", "dividend_payment"}:
        rows += _corporate(session, start, end, symbols)
    if "split" in wanted:
        rows += _splits(session, start, end, symbols)
    if wanted & {"fomc_meeting", "boc_rate_announcement", "data_release"}:
        rows += _economic(session, start, end)
    if "ipo" in wanted:
        rows += _ipos(session, start, end)
    order = {kind: index for index, kind in enumerate(KINDS)}
    kept = [row for row in rows if row.kind in wanted]
    return sorted(kept, key=lambda r: (r.day, order.get(r.kind, len(order)), r.subject))


def format_events(rows: list[EventRow]) -> list[str]:
    """Aligned text lines, one per event."""
    if not rows:
        return []
    kind_width = max(len(row.kind) for row in rows)
    subject_width = min(max(len(row.subject) for row in rows), 32)
    return [
        f"{row.day.isoformat()}  {row.kind:<{kind_width}}  "
        f"{row.subject[:subject_width]:<{subject_width}}  {row.detail}"
        for row in rows
    ]
