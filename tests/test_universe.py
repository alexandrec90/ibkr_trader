"""Point-in-time index universes: spans → gated Series, coverage, and the DB loader."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.backtest.engine import DELISTED_AFTER_DAYS, Series, curated_provenance
from ibkr_trader.backtest.universe import (
    SP500,
    Coverage,
    Span,
    coverage,
    UniverseSpec,
    index_provenance,
    load_index_universe,
    load_ipo_cohort,
    load_spans,
    load_universe_panel,
    member_ranges,
    with_ipo_cohort,
)
from ibkr_trader.db.models import Base, IndexMembership, Instrument, IpoEvent, PriceBar
from ibkr_trader.signals.dataset import UniverseSource

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


def _seed_two_member_index(session: Session) -> tuple[Instrument, Instrument, Instrument]:
    days = _days(120)
    live = _instrument_with_bars(session, "AAA", "SMART", days)
    dead = _instrument_with_bars(session, "TWTR", "NYSE", days[:60])
    etf = _instrument_with_bars(session, "SPY", "SMART", days)
    for symbol, end, iid, how in (
        ("AAA", None, live.id, "yahoo"),
        ("TWTR", D0 + timedelta(days=59), dead.id, "tiingo:TWTR"),
        ("ANTM", None, None, "unpriced"),
    ):
        session.add(
            IndexMembership(
                index_code=SP500,
                symbol=symbol,
                start_date=D0,
                end_date=end,
                source="fja05680",
                instrument_id=iid,
                resolution=how,
            )
        )
    session.flush()
    return live, dead, etf


def test_load_universe_panel_point_in_time_keeps_the_dead_and_stamps_coverage():
    session = _session()
    live, dead, etf = _seed_two_member_index(session)
    window = (datetime(2019, 6, 1, tzinfo=UTC), datetime(2020, 12, 31, tzinfo=UTC))

    loaded = load_universe_panel(session, ["spy"], window, index_code=SP500, measure_from=D0)

    assert set(loaded.series) == {live.id, dead.id, etf.id}  # the delisted member stays in
    stamp = loaded.provenance
    assert stamp["survivorship"] == "point-in-time"
    assert stamp["source"] == "index-point-in-time:SP500"
    assert stamp["extra_symbols"] == ["SPY"]
    assert stamp["n_symbols"] == 3
    assert stamp["unpriced_spans"] == 1 and stamp["pending_spans"] == 0
    assert 0.0 < stamp["coverage_ratio"] < 1.0  # ANTM was never priced
    assert len(stamp["sha256_16"]) == 16


def test_load_universe_panel_without_an_index_is_the_curated_list():
    session = _session()
    live, dead, etf = _seed_two_member_index(session)
    window = (datetime(2019, 6, 1, tzinfo=UTC), datetime(2020, 12, 31, tzinfo=UTC))

    loaded = load_universe_panel(session, ["AAA", "SPY"], window)

    assert set(loaded.series) == {live.id, etf.id}  # only what was asked for: no TWTR
    assert all(s.member_ranges is None for s in loaded.series.values())
    assert loaded.provenance == curated_provenance(["AAA", "SPY"])


def test_universe_spec_loads_through_the_dataset_protocol():
    session = _session()
    live, dead, etf = _seed_two_member_index(session)
    window = (datetime(2019, 6, 1, tzinfo=UTC), datetime(2020, 12, 31, tzinfo=UTC))
    spec = UniverseSpec(("SPY",), SP500)

    # signals.dataset reaches the spec structurally, never by importing this module
    assert isinstance(spec, UniverseSource)
    loaded = spec.load(session, window, measure_from=D0)
    assert loaded == load_universe_panel(
        session, ["SPY"], window, index_code=SP500, measure_from=D0
    )
    assert set(loaded.series) == {live.id, dead.id, etf.id}
    assert UniverseSpec(("AAA",)).load(session, window).provenance["survivorship"] == (
        "curated-current"
    )


def _ipo(
    session: Session,
    name: str,
    day: date,
    iid: int | None,
    resolution: str | None,
    *,
    source: str = "finnhub",
    stage: str = "priced",
):
    moment = datetime.combine(day, datetime.min.time(), UTC)
    session.add(
        IpoEvent(
            source=source,
            external_id=name,
            company_name=name,
            stage=stage,
            expected_date=day,
            instrument_id=iid,
            resolution=resolution,
            first_seen_at=moment,
            fetched_at=moment,
        )
    )
    session.flush()


def test_ipo_cohort_joins_the_index_ungated_with_its_dead_and_unpriced_counted():
    session = _session()
    live, dead, etf = _seed_two_member_index(session)
    fresh = _instrument_with_bars(session, "NEWCO", "NASDAQ", _days(60, D0 + timedelta(days=50)))
    flop = _instrument_with_bars(session, "FLOP", "NYSE", _days(20, D0 + timedelta(days=30)))
    _ipo(session, "Newco", D0 + timedelta(days=50), fresh.id, "yahoo")
    _ipo(session, "Flop", D0 + timedelta(days=30), flop.id, "tiingo:FLOP")  # delisted since
    _ipo(session, "Ghost", D0 + timedelta(days=40), None, "unpriced")
    _ipo(session, "Old", date(2001, 1, 1), None, "unpriced")  # outside the window: not counted
    _ipo(session, "Twitter", D0, dead.id, "tiingo:TWTR")  # an IPO that also joined the index
    _ipo(session, "Filing", D0, None, None, source="sec_edgar", stage="filed")  # an S-1
    _ipo(session, "Shell", D0 + timedelta(days=45), None, "excluded:spac")  # not common stock
    _ipo(session, "Deferred", D0 + timedelta(days=60), None, None)  # waiting for Tiingo budget
    window = (datetime(2019, 6, 1, tzinfo=UTC), datetime(2020, 12, 31, tzinfo=UTC))

    plain = UniverseSpec(("SPY",), SP500).load(session, window)
    loaded = UniverseSpec(("SPY",), SP500, ipo_cohort=True).load(session, window)

    assert set(loaded.series) == {live.id, dead.id, etf.id, fresh.id, flop.id}
    assert loaded.series[fresh.id].member_ranges is None  # a candidate from its listing on
    assert loaded.series[dead.id].member_ranges is None  # the cohort outranks the index gate
    assert plain.series[dead.id].member_ranges is not None
    stamp = loaded.provenance
    assert stamp["source"] == "index-point-in-time:SP500+ipo-cohort"
    assert stamp["survivorship"] == "point-in-time"
    assert stamp["ipo_cohort"] == {
        "instruments": 3,
        "unpriced_listings": 1,  # Ghost; Old is outside the window
        "pending_listings": 1,  # Deferred; the S-1 is not a listing at all
    }
    assert stamp["n_symbols"] == 5
    assert stamp["sha256_16"] != plain.provenance["sha256_16"]
    assert with_ipo_cohort(session, plain, window) == loaded  # what the spec's flag adds


def test_ipo_cohort_on_a_curated_list_stays_survivor_biased():
    session = _session()
    _seed_two_member_index(session)
    window = (datetime(2019, 6, 1, tzinfo=UTC), datetime(2020, 12, 31, tzinfo=UTC))
    loaded = UniverseSpec(("AAA",), ipo_cohort=True).load(session, window)
    assert loaded.provenance["survivorship"] == "curated-current"
    assert loaded.provenance["ipo_cohort"] == {
        "instruments": 0,
        "unpriced_listings": 0,
        "pending_listings": 0,
    }


def test_a_stored_listing_date_reaches_the_series():
    session = _session()
    listed = _instrument_with_bars(session, "NEWCO", "NASDAQ", _days(30, D0))
    listed.listed_on = D0
    unknown = _instrument_with_bars(session, "OLDCO", "NYSE", _days(30, D0))
    window = (datetime(2019, 6, 1, tzinfo=UTC), datetime(2020, 12, 31, tzinfo=UTC))
    series = load_universe_panel(session, ["NEWCO", "OLDCO"], window).series
    assert series[listed.id].listed_on == D0
    assert series[unknown.id].listed_on is None


def test_load_ipo_cohort_skips_listings_with_no_bars_in_the_window():
    session = _session()
    later = _instrument_with_bars(session, "LATER", "NYSE", _days(10, date(2024, 1, 1)))
    _ipo(session, "Later", date(2024, 1, 1), later.id, "yahoo")
    window = (datetime(2019, 6, 1, tzinfo=UTC), datetime(2020, 12, 31, tzinfo=UTC))
    assert load_ipo_cohort(session, window) == (
        {},
        {"unpriced_listings": 0, "pending_listings": 0},
    )


def test_index_provenance_hash_tracks_the_instruments_loaded():
    days = _days(5)
    one = {1: _series(1, days)}
    two = {1: _series(1, days), 2: _series(2, days)}
    a = index_provenance(SP500, [], one, Coverage())
    b = index_provenance(SP500, [], two, Coverage())
    assert a["sha256_16"] != b["sha256_16"]
    assert a["coverage_ratio"] == 1.0
