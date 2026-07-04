from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.db.models import Base, Instrument, PriceBar
from ibkr_trader.ingestion.market import fmp


class FakeResponse:
    def __init__(self, payload: object):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


@pytest.fixture(autouse=True)
def clear_settings_cache():
    fmp.get_settings.cache_clear()
    yield
    fmp.get_settings.cache_clear()


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


def test_fmp_fetch_upserts_price_bars(monkeypatch):
    payload = [
        {"date": "2024-01-03", "open": 189.0, "high": 191.0, "low": 188.5, "close": 190.2},
        {
            "date": "2024-01-02",
            "open": 187.1,
            "high": 189.4,
            "low": 186.8,
            "close": 188.9,
            "volume": 1000,
        },
    ]
    session_cm = _make_session_scope()

    def fake_get(url, params, timeout):
        assert url == f"{fmp.BASE_URL}/historical-price-eod/full"
        assert params["symbol"] == "AAPL"
        assert params["apikey"] == "test-fmp-key"
        assert timeout == 30
        return FakeResponse(payload)

    monkeypatch.setenv("FMP_KEY", "test-fmp-key")
    monkeypatch.setattr(fmp.httpx, "get", fake_get)
    monkeypatch.setattr(fmp, "get_session", session_cm)

    count = fmp.FmpConnector().fetch(symbol="aapl")

    assert count == 2
    with session_cm() as session:
        instrument = session.scalar(select(Instrument).where(Instrument.symbol == "AAPL"))
        assert instrument is not None
        bars = session.scalars(
            select(PriceBar).where(PriceBar.instrument_id == instrument.id).order_by(PriceBar.ts)
        ).all()
        assert len(bars) == 2
        assert bars[0].source == "fmp"
        assert bars[0].bar_size == "1 day"
        assert bars[0].open == 187.1
        assert bars[0].volume == 1000.0
        assert bars[1].volume is None


def test_fmp_fetch_updates_existing_price_bar(monkeypatch):
    first_payload = [
        {"date": "2024-01-02", "open": 187.1, "high": 189.4, "low": 186.8, "close": 188.9}
    ]
    second_payload = [
        {"date": "2024-01-02", "open": 187.1, "high": 190.0, "low": 186.8, "close": 189.5}
    ]
    payloads = [first_payload, second_payload]
    session_cm = _make_session_scope()

    def fake_get(url, params, timeout):
        return FakeResponse(payloads.pop(0))

    monkeypatch.setenv("FMP_KEY", "test-fmp-key")
    monkeypatch.setattr(fmp.httpx, "get", fake_get)
    monkeypatch.setattr(fmp, "get_session", session_cm)

    assert fmp.FmpConnector().fetch(symbol="AAPL") == 1
    assert fmp.FmpConnector().fetch(symbol="AAPL") == 1

    with session_cm() as session:
        bars = session.scalars(select(PriceBar)).all()
        assert len(bars) == 1
        assert bars[0].high == 190.0
        assert bars[0].close == 189.5
