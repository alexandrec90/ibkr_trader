"""Point-in-time index universes: spans → gated Series, coverage, and the DB loader."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.backtest.engine import DELISTED_AFTER_DAYS, Series
from ibkr_trader.backtest.universe import (
    SP500,
    Coverage,
    Span,
    coverage,
    load_index_universe,
    load_spans,
    member_ranges,
)
from ibkr_trader.db.models import Base, IndexMembership, Instrument, PriceBar

D0 = date(2020, 1, 1)


def _days(n: int, start: date = D0) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _series(iid: int, days: list[date]) -> Series:
    ones = [1.0] * len(days)
    return Series(iid, f"S{iid}", "USD", "SMART", days, ones, ones, ones)


def test_span_contains_and_overlaps_are_inclusive():
    span = Span("A", D0, D0 + timedelta(days=9))
    assert span.contains(D0) and span.contains(D0 + timedelta(days=9))
    assert not span.contains(D0 + timedelta(days=10))
    assert span.overlaps(D0 + timedelta(days=9), D0 + timedelta(days=20))
    assert not span.overlaps(D0 + timedelta(days=10), D0 + timedelta(days=20))
    assert Span("B", D0, None).contains(date(2099, 1, 1))


def test_member_ranges_group_priced_spans_by_instrument_and_drop_unpriced():
    later = D0 + timedelta(days=400)
    spans = [
        Span("AAA", later, None, instrument_id=1),
        Span("AAA", D0, D0 + timedelta(days=100), instrument_id=1),  # dropped, re-added
        Span("BBBY", D0, D0 + timedelta(days=50), instrument_id=2, resolution="tiingo:BBBYQ"),
        Span("ANTM", D0, None, instrument_id=None, resolution="unpriced"),
    ]
    assert member_ranges(spans) == {
        1: ((D0, D0 + timedelta(days=100)), (later, None)),
        2: ((D0, D0 + timedelta(days=50)),),
    }


def test_coverage_counts_only_members_whose_bars_are_actually_there():
    days = _days(200)
    universe = {
        1: _series(1, days),  # priced throughout
        2: _series(2, days[:40]),  # bars stop on day 39: delisted
    }
    spans = [
        Span("A", D0, None, instrument_id=1),
        Span("B", D0, None, instrument_id=2),  # the index kept it longer than its bars
        Span("C", D0, D0 + timedelta(days=100), instrument_id=None, resolution="unpriced"),
        Span("Z", D0 + timedelta(days=500), None, instrument_id=9),  # outside the window
    ]
    result = coverage(spans, universe, D0, D0 + timedelta(days=150), step_days=50)
    # day 0: A,B,C members, A,B priced; day 50: B has ended (>10 days past its last bar)
    assert DELISTED_AFTER_DAYS < 50 - 39
    assert result.samples == [
        (D0, 3, 2),
        (D0 + timedelta(days=50), 3, 1),
        (D0 + timedelta(days=100), 3, 1),
        (D0 + timedelta(days=150), 2, 1),
    ]
    assert [s.symbol for s in result.unpriced] == ["C"]
    assert result.ratio == pytest.approx(5 / 11)
    assert result.ratio_since(D0 + timedelta(days=100)) == pytest.approx(2 / 5)


def test_coverage_separates_unpriceable_spans_from_not_yet_attempted_ones():
    spans = [
        Span("DEAD", D0, None, instrument_id=None, resolution="unpriced"),
        Span("TODO", D0, None, instrument_id=None, resolution=None),
        Span("DONE", D0, None, instrument_id=1, resolution="yahoo"),
    ]
    result = coverage(spans, {1: _series(1, _days(5))}, D0, D0, step_days=1)
    assert [s.symbol for s in result.unpriced] == ["DEAD"]
    assert [s.symbol for s in result.pending] == ["TODO"]


def test_empty_coverage_reads_as_complete():
    assert Coverage().ratio == 1.0


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _instrument_with_bars(session: Session, symbol: str, exchange: str, days: list[date]):
    instrument = Instrument(symbol=symbol, exchange=exchange, currency="USD")
    session.add(instrument)
    session.flush()
    for day in days:
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                ts=datetime.combine(day, datetime.min.time(), UTC),
                bar_size="1 day",
                source="yahoo",
                what_to_show="ADJUSTED_LAST",
                open=10.0,
                high=10.0,
                low=10.0,
                close=10.0,
                volume=1_000.0,
            )
        )
    return instrument


def test_load_index_universe_gates_members_and_adds_ungated_extras():
    session = _session()
    days = _days(120)
    live = _instrument_with_bars(session, "AAA", "SMART", days)
    dead = _instrument_with_bars(session, "TWTR", "NYSE", days[:60])
    etf = _instrument_with_bars(session, "SPY", "SMART", days)
    rows = [
        ("AAA", D0, None, live.id, "yahoo"),
        ("TWTR", D0, D0 + timedelta(days=59), dead.id, "tiingo:TWTR"),
        ("ANTM", D0, None, None, "unpriced"),
        ("OLD", date(2001, 1, 1), date(2005, 1, 1), live.id, "yahoo"),  # before the window
        ("ZZZ", D0, None, live.id, "yahoo"),  # another index: never read
    ]
    for symbol, start, end, iid, how in rows:
        session.add(
            IndexMembership(
                index_code="OTHER" if symbol == "ZZZ" else SP500,
                symbol=symbol,
                start_date=start,
                end_date=end,
                source="fja05680",
                instrument_id=iid,
                resolution=how,
            )
        )
    session.flush()
    window = (datetime(2019, 6, 1, tzinfo=UTC), datetime(2020, 12, 31, tzinfo=UTC))

    spans = load_spans(session, SP500, window[0].date(), window[1].date())
    assert sorted(s.symbol for s in spans) == ["AAA", "ANTM", "TWTR"]

    universe, loaded_spans = load_index_universe(session, SP500, window, ["SPY", "NOPE"])
    assert set(universe) == {live.id, dead.id, etf.id}
    assert universe[live.id].member_ranges == ((D0, None),)
    assert universe[dead.id].member_ranges == ((D0, D0 + timedelta(days=59)),)
    assert universe[etf.id].member_ranges is None  # ETFs are never index-gated
    assert {s.symbol for s in loaded_spans} == {"AAA", "ANTM", "TWTR"}
    measured = coverage(loaded_spans, universe, D0, D0 + timedelta(days=90), step_days=90)
    assert measured.samples == [(D0, 3, 2), (D0 + timedelta(days=90), 2, 1)]
