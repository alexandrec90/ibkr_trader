from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.db.models import Base, Instrument, PriceBar
from ibkr_trader.ingestion.market import yahoo


def _make_session_scope():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return scope


def _frame(rows: dict[str, list], dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, index=pd.DatetimeIndex(dates, tz="America/Toronto"))


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(yahoo, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(yahoo, "_last_request_monotonic", None)


def test_yahoo_fetch_upserts_price_bars_and_maps_to_suffix(monkeypatch):
    frame = _frame(
        {
            "Open": [30.0, 30.5],
            "High": [30.6, 31.0],
            "Low": [29.8, 30.2],
            "Close": [30.4, 30.9],
            "Volume": [100_000, float("nan")],
        },
        ["2024-01-02", "2024-01-03"],
    )
    calls: list[tuple] = []
    session_cm = _make_session_scope()

    def fake_download(symbol, start, end, auto_adjust):
        calls.append((symbol, start, end, auto_adjust))
        return frame

    monkeypatch.setattr(yahoo, "_download_history", fake_download)
    monkeypatch.setattr(yahoo, "get_session", session_cm)

    count = yahoo.YahooConnector().fetch(symbol="xeqt.to")

    assert count == 2
    assert calls == [("XEQT.TO", None, None, True)]
    with session_cm() as session:
        instrument = session.scalar(select(Instrument).where(Instrument.symbol == "XEQT"))
        assert instrument is not None
        assert instrument.exchange == "TSX"
        assert instrument.currency == "CAD"
        bars = session.scalars(
            select(PriceBar).where(PriceBar.instrument_id == instrument.id).order_by(PriceBar.ts)
        ).all()
        assert len(bars) == 2
        assert bars[0].source == "yahoo"
        assert bars[0].what_to_show == "ADJUSTED_LAST"
        assert bars[0].bar_size == "1 day"
        assert bars[0].ts.date() == date(2024, 1, 2)
        assert bars[0].open == 30.0
        assert bars[0].volume == 100_000.0
        assert bars[1].volume is None


def test_yahoo_fetch_starts_from_next_missing_date(monkeypatch):
    frames = [
        _frame(
            {"Open": [30.0], "High": [30.6], "Low": [29.8], "Close": [30.4], "Volume": [1.0]},
            ["2024-01-02"],
        ),
        _frame(
            {"Open": [30.5], "High": [31.0], "Low": [30.2], "Close": [30.9], "Volume": [2.0]},
            ["2024-01-03"],
        ),
    ]
    starts: list = []
    session_cm = _make_session_scope()

    def fake_download(symbol, start, end, auto_adjust):
        starts.append(start)
        return frames.pop(0)

    monkeypatch.setattr(yahoo, "_download_history", fake_download)
    monkeypatch.setattr(yahoo, "get_session", session_cm)

    assert yahoo.YahooConnector().fetch(symbol="XEQT.TO") == 1
    assert yahoo.YahooConnector().fetch(symbol="XEQT.TO") == 1

    assert starts == [None, date(2024, 1, 3)]
    with session_cm() as session:
        assert len(session.scalars(select(PriceBar)).all()) == 2


def test_yahoo_fetch_updates_existing_price_bar(monkeypatch):
    frames = [
        _frame(
            {"Open": [30.0], "High": [30.6], "Low": [29.8], "Close": [30.4], "Volume": [1.0]},
            ["2024-01-02"],
        ),
        _frame(
            {"Open": [30.0], "High": [30.8], "Low": [29.8], "Close": [30.5], "Volume": [1.0]},
            ["2024-01-02"],
        ),
    ]
    session_cm = _make_session_scope()

    monkeypatch.setattr(yahoo, "_download_history", lambda *a, **k: frames.pop(0))
    monkeypatch.setattr(yahoo, "get_session", session_cm)

    assert yahoo.YahooConnector().fetch(symbol="XEQT.TO") == 1
    assert yahoo.YahooConnector().fetch(symbol="XEQT.TO", date_from="2024-01-02") == 1

    with session_cm() as session:
        bars = session.scalars(select(PriceBar)).all()
        assert len(bars) == 1
        assert bars[0].high == 30.8
        assert bars[0].close == 30.5


def test_yahoo_fetch_skips_request_when_local_data_covers_date_to(monkeypatch):
    session_cm = _make_session_scope()

    def fail_download(*args, **kwargs):
        raise AssertionError("Yahoo should not be called when local data already covers date_to")

    monkeypatch.setattr(yahoo, "_download_history", fail_download)
    monkeypatch.setattr(yahoo, "get_session", session_cm)

    with session_cm() as session:
        instrument = Instrument(symbol="XEQT", exchange="TSX", currency="CAD")
        session.add(instrument)
        session.flush()
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                ts=yahoo._daily_ts(date(2024, 1, 2)),
                bar_size="1 day",
                source="yahoo",
                what_to_show="ADJUSTED_LAST",
                open=30.0,
                high=30.6,
                low=29.8,
                close=30.4,
                volume=1.0,
            )
        )

    assert yahoo.YahooConnector().fetch(symbol="XEQT.TO", date_to="2024-01-02") == 0


def test_yahoo_fetch_trades_disables_auto_adjust_and_passes_exclusive_end(monkeypatch):
    calls: list[tuple] = []
    session_cm = _make_session_scope()

    def fake_download(symbol, start, end, auto_adjust):
        calls.append((symbol, start, end, auto_adjust))
        return _frame(
            {"Open": [30.0], "High": [30.6], "Low": [29.8], "Close": [30.4], "Volume": [1.0]},
            ["2024-01-02"],
        )

    monkeypatch.setattr(yahoo, "_download_history", fake_download)
    monkeypatch.setattr(yahoo, "get_session", session_cm)

    connector = yahoo.YahooConnector()
    count = connector.fetch(
        symbol="GOOG", date_from="2024-01-02", date_to="2024-01-05", what_to_show="TRADES"
    )

    assert count == 1
    # yfinance treats `end` as exclusive, so an inclusive date_to becomes date_to + 1 day
    assert calls == [("GOOG", date(2024, 1, 2), date(2024, 1, 6), False)]
    with session_cm() as session:
        instrument = session.scalar(select(Instrument).where(Instrument.symbol == "GOOG"))
        assert instrument is not None
        assert instrument.exchange == "SMART"
        assert instrument.currency == "USD"
        bar = session.scalar(select(PriceBar))
        assert bar is not None
        assert bar.what_to_show == "TRADES"


def test_yahoo_fetch_skips_request_when_already_current(monkeypatch):
    """DB current through today → next missing date is in the future → don't call Yahoo."""
    from datetime import UTC, datetime

    session_cm = _make_session_scope()

    def fail_download(*args, **kwargs):
        raise AssertionError("Yahoo should not be called when the next missing date is future")

    monkeypatch.setattr(yahoo, "_download_history", fail_download)
    monkeypatch.setattr(yahoo, "get_session", session_cm)

    today = datetime.now(tz=UTC).date()
    with session_cm() as session:
        instrument = Instrument(symbol="XEQT", exchange="TSX", currency="CAD")
        session.add(instrument)
        session.flush()
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                ts=yahoo._daily_ts(today),
                bar_size="1 day",
                source="yahoo",
                what_to_show="ADJUSTED_LAST",
                open=30.0,
                high=30.6,
                low=29.8,
                close=30.4,
                volume=1.0,
            )
        )

    assert yahoo.YahooConnector().fetch(symbol="XEQT.TO") == 0


def test_yahoo_fetch_wraps_provider_errors(monkeypatch):
    session_cm = _make_session_scope()

    def fake_download(*args, **kwargs):
        raise RuntimeError("possibly rate limited")

    monkeypatch.setattr(yahoo, "_download_history", fake_download)
    monkeypatch.setattr(yahoo, "get_session", session_cm)

    with pytest.raises(yahoo.YahooProviderError) as exc_info:
        yahoo.YahooConnector().fetch(symbol="XEQT.TO")
    assert "XEQT.TO" in str(exc_info.value)


def test_throttle_spaces_out_consecutive_requests(monkeypatch):
    clock = {"now": 0.0}
    sleeps: list[float] = []

    monkeypatch.setattr(yahoo, "MIN_REQUEST_INTERVAL_SECONDS", 2.0)
    monkeypatch.setattr(yahoo, "_last_request_monotonic", None)
    monkeypatch.setattr(yahoo.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(yahoo.time, "sleep", sleeps.append)

    yahoo._throttle()
    assert sleeps == []  # first request goes straight through

    clock["now"] = 0.5
    yahoo._throttle()
    assert sleeps == [1.5]  # only 0.5s elapsed → wait the remaining 1.5s
