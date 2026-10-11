from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ibkr_trader.db.models import (
    Base,
    CorporateEvent,
    EarningsEvent,
    EconomicEvent,
    Instrument,
    IpoEvent,
    StockSplit,
)
from ibkr_trader.events import (
    KINDS,
    EventRow,
    earnings_detail,
    economic_detail,
    format_events,
    load_events,
    split_detail,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _instrument(session, symbol):
    instrument = Instrument(symbol=symbol)
    session.add(instrument)
    session.flush()
    return instrument


def _economic(kind, name, day, country="US", raw=None, external_id=None):
    return EconomicEvent(
        source="test",
        external_id=external_id or f"{kind}:{day}",
        kind=kind,
        name=name,
        country=country,
        event_date=day,
        raw=raw,
        first_seen_at=NOW,
        fetched_at=NOW,
    )


@pytest.fixture
def stocked(session):
    aapl = _instrument(session, "AAPL")
    msft = _instrument(session, "MSFT")
    session.add_all(
        [
            # One report on two sources: merged into one event, each field from the earliest
            # row that has it (Finnhub's date-only 00:00 sorts first, so its estimate wins).
            EarningsEvent(
                instrument_id=aapl.id,
                report_ts=datetime(2026, 10, 29, 20, 30, tzinfo=UTC),
                source="yahoo",
                eps_estimate=1.75,
            ),
            EarningsEvent(
                instrument_id=aapl.id,
                report_ts=datetime(2026, 10, 29, tzinfo=UTC),
                source="finnhub",
                hour="amc",
                eps_estimate=1.70,
            ),
            EarningsEvent(
                instrument_id=msft.id,
                report_ts=datetime(2026, 7, 28, 20, 0, tzinfo=UTC),
                source="yahoo",
                eps_estimate=3.35,
                eps_actual=3.65,
                surprise_pct=8.96,
            ),
            CorporateEvent(
                instrument_id=msft.id,
                kind="ex_dividend",
                event_date=date(2026, 11, 19),
                source="yahoo",
                first_seen_at=NOW,
                fetched_at=NOW,
            ),
            StockSplit(instrument_id=aapl.id, ex_date=date(2020, 8, 31), ratio=4.0, source="yahoo"),
            _economic("fomc_meeting", "FOMC meeting", date(2026, 10, 28), raw={"sep": False}),
            _economic(
                "boc_rate_announcement",
                "Bank of Canada rate announcement",
                date(2026, 10, 28),
                country="CA",
                raw={"mpr": True},
            ),
            _economic("data_release", "Consumer Price Index", date(2026, 10, 15)),
            IpoEvent(
                source="finnhub",
                external_id="h1",
                company_name="Discord Inc",
                stage="expected",
                symbol="DCRD",
                expected_date=date(2026, 10, 20),
                is_spac=None,
                first_seen_at=NOW,
                fetched_at=NOW,
            ),
        ]
    )
    session.flush()
    return session


def test_window_lists_every_kind_in_day_then_kind_order(stocked):
    rows = load_events(stocked, date(2026, 10, 10), date(2026, 11, 30))
    assert [(r.day, r.kind, r.subject) for r in rows] == [
        (date(2026, 10, 15), "data_release", "US"),
        (date(2026, 10, 20), "ipo", "Discord Inc"),
        (date(2026, 10, 28), "fomc_meeting", "US"),
        (date(2026, 10, 28), "boc_rate_announcement", "CA"),
        (date(2026, 10, 29), "earnings", "AAPL"),
        (date(2026, 11, 19), "ex_dividend", "MSFT"),
    ]


def test_two_sources_for_one_report_merge_into_one_event(stocked):
    rows = load_events(stocked, date(2026, 10, 29), date(2026, 10, 29), kinds=["earnings"])
    assert rows == [EventRow(date(2026, 10, 29), "earnings", "AAPL", "after close, EPS est 1.70")]


def test_end_day_is_inclusive_and_history_comes_back(stocked):
    rows = load_events(stocked, date(2026, 7, 28), date(2026, 7, 28))
    assert rows == [EventRow(date(2026, 7, 28), "earnings", "MSFT", "EPS 3.65 vs est 3.35 (+9.0%)")]
    splits = load_events(stocked, date(2020, 1, 1), date(2020, 12, 31), kinds=["split"])
    assert splits == [EventRow(date(2020, 8, 31), "split", "AAPL", "4-for-1 split")]


def test_symbols_narrow_per_instrument_kinds_but_keep_macro(stocked):
    rows = load_events(stocked, date(2026, 10, 10), date(2026, 11, 30), symbols=["MSFT"])
    kinds = {(r.kind, r.subject) for r in rows}
    assert ("earnings", "AAPL") not in kinds
    assert ("ex_dividend", "MSFT") in kinds
    assert ("fomc_meeting", "US") in kinds and ("ipo", "Discord Inc") in kinds


def test_kinds_filter_and_unknown_kind(stocked):
    rows = load_events(stocked, date(2026, 10, 10), date(2026, 11, 30), kinds=["fomc_meeting"])
    assert [r.kind for r in rows] == ["fomc_meeting"]
    with pytest.raises(ValueError, match="earnigns"):
        load_events(stocked, date(2026, 10, 10), date(2026, 11, 30), kinds=["earnigns"])


def test_empty_window(session):
    assert load_events(session, date(2026, 1, 1), date(2026, 1, 31)) == []
    assert format_events([]) == []


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("bmo", 1.2, None, None), "before open, EPS est 1.20"),
        ((None, 1.2, 1.5, 25.0), "EPS 1.50 vs est 1.20 (+25.0%)"),
        ((None, None, 1.5, None), "EPS 1.50"),
        (("odd", None, None, None), "earnings report"),
        (("dmh", None, None, None), "during market hours"),
    ],
)
def test_earnings_detail(args, expected):
    assert earnings_detail(*args) == expected


def test_split_detail_forward_and_reverse():
    assert split_detail(4.0) == "4-for-1 split"
    assert split_detail(1.5) == "1.5-for-1 split"
    assert split_detail(0.1) == "1-for-10 reverse split"


def test_economic_detail_flags():
    assert economic_detail("fomc_meeting", "FOMC", {"sep": True}) == "FOMC (with projections)"
    assert economic_detail("fomc_meeting", "FOMC", {"notation_vote": True}) == (
        "FOMC (notation vote)"
    )
    assert economic_detail("boc_rate_announcement", "BoC", {"mpr": True}) == (
        "BoC (with Monetary Policy Report)"
    )
    assert economic_detail("data_release", "CPI", None) == "CPI"


def test_ipo_detail_labels_a_spac(session):
    session.add(
        IpoEvent(
            source="finnhub",
            external_id="s",
            company_name="Blank Acquisition Corp",
            stage="priced",
            expected_date=date(2026, 10, 12),
            is_spac=True,
            first_seen_at=NOW,
            fetched_at=NOW,
        )
    )
    session.flush()
    (row,) = load_events(session, date(2026, 10, 12), date(2026, 10, 12))
    assert row.detail == "IPO priced (SPAC)"


def test_format_aligns_columns():
    lines = format_events(
        [
            EventRow(date(2026, 10, 15), "data_release", "US", "Consumer Price Index"),
            EventRow(date(2026, 10, 29), "earnings", "AAPL", "after close"),
        ]
    )
    assert lines == [
        "2026-10-15  data_release  US    Consumer Price Index",
        "2026-10-29  earnings      AAPL  after close",
    ]


def test_kinds_are_unique():
    assert len(KINDS) == len(set(KINDS))
