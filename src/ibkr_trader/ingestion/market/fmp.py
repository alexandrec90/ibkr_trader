"""Financial Modeling Prep connector — EOD prices, fundamentals, calendars.

Free tier ~250 req/day. Docs:
https://site.financialmodelingprep.com/developer/docs/stable/historical-price-eod-full
"""

from datetime import UTC, datetime, time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ibkr_trader.config import get_settings
from ibkr_trader.db.models import Instrument, PriceBar
from ibkr_trader.db.session import get_session
from ibkr_trader.ingestion.base import Connector

BASE_URL = "https://financialmodelingprep.com/stable"


def _instrument_defaults(fmp_symbol: str) -> tuple[str, str, str]:
    """Map common FMP symbols to the canonical instrument key used locally."""
    symbol = fmp_symbol.strip().upper()
    if symbol.endswith(".TO"):
        return symbol.removesuffix(".TO"), "TSX", "CAD"
    return symbol, "SMART", "USD"


def _daily_ts(value: str) -> datetime:
    day = datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.combine(day, time.min, tzinfo=UTC)


def _historical_rows(payload: Any) -> list[dict[str, Any]]:
    """Accept both current stable responses and the older v3 envelope shape."""
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("historical"), list):
        rows = payload["historical"]
    else:
        raise ValueError("unexpected FMP historical-price response shape")

    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("unexpected FMP historical-price row shape")
    return rows


def _bar_values(row: dict[str, Any]) -> dict[str, Any]:
    try:
        volume = row.get("volume")
        return {
            "ts": _daily_ts(str(row["date"])),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": None if volume is None else float(volume),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid FMP price bar row: {row!r}") from exc


def _get_or_create_instrument(session: Session, fmp_symbol: str) -> Instrument:
    symbol, exchange, currency = _instrument_defaults(fmp_symbol)
    instrument = session.scalar(
        select(Instrument).where(
            Instrument.symbol == symbol,
            Instrument.exchange == exchange,
            Instrument.currency == currency,
        )
    )
    if instrument:
        return instrument

    instrument = Instrument(symbol=symbol, exchange=exchange, currency=currency)
    session.add(instrument)
    session.flush()
    return instrument


class FmpConnector(Connector):
    name = "fmp"

    def fetch(
        self,
        symbol: str = "",
        date_from: str = "",
        date_to: str = "",
        bar_size: str = "1 day",
        what_to_show: str = "TRADES",
        **kwargs,
    ) -> int:
        settings = get_settings()
        if not settings.fmp_key:
            raise RuntimeError("FMP_KEY is not set (see .env.example)")
        fmp_symbol = symbol.strip().upper()
        if not fmp_symbol:
            raise ValueError("symbol is required")

        params = {"symbol": fmp_symbol, "apikey": settings.fmp_key}
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to
        response = httpx.get(
            f"{BASE_URL}/historical-price-eod/full",
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        rows = sorted(_historical_rows(response.json()), key=lambda row: str(row.get("date", "")))

        with get_session() as session:
            instrument = _get_or_create_instrument(session, fmp_symbol)
            count = 0
            for row in rows:
                values = _bar_values(row)
                existing = session.scalar(
                    select(PriceBar).where(
                        PriceBar.instrument_id == instrument.id,
                        PriceBar.ts == values["ts"],
                        PriceBar.bar_size == bar_size,
                        PriceBar.source == self.name,
                        PriceBar.what_to_show == what_to_show,
                    )
                )
                if existing:
                    existing.open = values["open"]
                    existing.high = values["high"]
                    existing.low = values["low"]
                    existing.close = values["close"]
                    existing.volume = values["volume"]
                else:
                    session.add(
                        PriceBar(
                            instrument_id=instrument.id,
                            ts=values["ts"],
                            bar_size=bar_size,
                            source=self.name,
                            what_to_show=what_to_show,
                            open=values["open"],
                            high=values["high"],
                            low=values["low"],
                            close=values["close"],
                            volume=values["volume"],
                        )
                    )
                count += 1
            return count
