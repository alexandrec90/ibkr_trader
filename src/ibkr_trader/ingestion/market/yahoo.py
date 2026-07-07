"""Yahoo Finance connector (via yfinance) — EOD prices for symbols FMP's free tier gates.

Yahoo has no official API: yfinance scrapes web endpoints, so treat it like pytrends —
unofficial and fragile. Yahoo temp-bans IPs that hammer it, hence the mandatory throttle
below. Keep volume tiny (a handful of symbols, once a day) and never put this connector
in a tight loop. Coverage includes TSX (`.TO` suffix), which FMP's free tier lacks.
"""

import threading
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as time_of_day
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ibkr_trader.db.models import Instrument, PriceBar
from ibkr_trader.db.session import get_session
from ibkr_trader.ingestion.base import Connector

#: Minimum spacing between Yahoo requests. There is no documented quota — Yahoo just
#: rate-limits/bans abusive IPs, so stay far below anything that looks automated-hostile.
MIN_REQUEST_INTERVAL_SECONDS = 2.0

#: Hard wall-clock ceiling on a single yfinance download. yfinance (via curl_cffi) exposes no
#: timeout, so when Yahoo rate-limits it can hold a request open indefinitely — hanging the whole
#: batch. The watchdog below abandons a request past this and raises, so the batch runner reports
#: a per-symbol failure and moves on instead of stalling forever.
DOWNLOAD_TIMEOUT_SECONDS = 45.0

_last_request_monotonic: float | None = None


class YahooProviderError(RuntimeError):
    """Yahoo rejected the request or returned no usable data."""


def _throttle() -> None:
    """Sleep as needed so consecutive Yahoo requests are at least the min interval apart."""
    global _last_request_monotonic
    now = time.monotonic()
    if _last_request_monotonic is not None:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (now - _last_request_monotonic)
        if wait > 0:
            time.sleep(wait)
    _last_request_monotonic = time.monotonic()


def _instrument_defaults(yahoo_symbol: str) -> tuple[str, str, str]:
    """Map Yahoo symbols to the canonical instrument key (same convention as fmp.py)."""
    symbol = yahoo_symbol.strip().upper()
    if symbol.endswith(".TO"):
        return symbol.removesuffix(".TO"), "TSX", "CAD"
    return symbol, "SMART", "USD"


def _daily_ts(day: date) -> datetime:
    return datetime.combine(day, time_of_day.min, tzinfo=UTC)


def _date_from_ymd(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _download_history(
    symbol: str,
    start: date | None,
    end: date | None,
    auto_adjust: bool,
) -> pd.DataFrame:
    """One yfinance history call. Isolated so tests can stub it without network."""
    import yfinance as yf
    from yfinance.exceptions import YFPricesMissingError

    try:
        return yf.Ticker(symbol).history(
            period=None if start else "max",
            interval="1d",
            start=start,
            end=end,
            auto_adjust=auto_adjust,
            actions=False,
            raise_errors=True,
        )
    except YFPricesMissingError:
        # Normal when the range holds no trading days (weekend/holiday incremental run).
        return pd.DataFrame()


def _download_within_timeout(
    symbol: str,
    start: date | None,
    end: date | None,
    auto_adjust: bool,
    timeout: float,
) -> pd.DataFrame:
    """Run ``_download_history`` under a wall-clock ceiling. Raises ``YahooProviderError`` if it
    doesn't return in time, so a request Yahoo has silently parked can't hang the batch.

    The work runs in a daemon thread: if it overruns we abandon it (it dies with the process)
    rather than blocking on ``join``. yfinance has no timeout knob, so this is the only reliable
    way to bound it. Tests stub ``_download_history``, so they return well within the ceiling.
    """
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["frame"] = _download_history(symbol, start, end, auto_adjust)
        except BaseException as exc:  # re-raised on the caller thread below
            box["error"] = exc

    worker = threading.Thread(target=_worker, name=f"yahoo-dl-{symbol}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise YahooProviderError(
            f"Yahoo request for {symbol} exceeded {timeout:.0f}s (likely rate-limited) — abandoned"
        )
    if "error" in box:
        raise YahooProviderError(f"Yahoo request for {symbol} failed: {box['error']}") from box[
            "error"
        ]
    return box["frame"]


def _float_or(value: Any, fallback: float | None) -> float | None:
    return fallback if value is None or pd.isna(value) else float(value)


def _bar_values(frame: pd.DataFrame) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for idx, row in frame.iterrows():
        close = _float_or(row.get("Close"), None)
        if close is None:
            continue
        values.append(
            {
                "ts": _daily_ts(idx.date()),
                "open": _float_or(row.get("Open"), close),
                "high": _float_or(row.get("High"), close),
                "low": _float_or(row.get("Low"), close),
                "close": close,
                "volume": _float_or(row.get("Volume"), None),
            }
        )
    return values


def _get_or_create_instrument(session: Session, yahoo_symbol: str) -> Instrument:
    symbol, exchange, currency = _instrument_defaults(yahoo_symbol)
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


def _get_instrument(session: Session, yahoo_symbol: str) -> Instrument | None:
    symbol, exchange, currency = _instrument_defaults(yahoo_symbol)
    return session.scalar(
        select(Instrument).where(
            Instrument.symbol == symbol,
            Instrument.exchange == exchange,
            Instrument.currency == currency,
        )
    )


def _next_missing_date(
    session: Session,
    instrument: Instrument,
    bar_size: str,
    source: str,
    what_to_show: str,
) -> date | None:
    latest_ts = session.scalar(
        select(func.max(PriceBar.ts)).where(
            PriceBar.instrument_id == instrument.id,
            PriceBar.bar_size == bar_size,
            PriceBar.source == source,
            PriceBar.what_to_show == what_to_show,
        )
    )
    if latest_ts is None:
        return None
    return latest_ts.date() + timedelta(days=1)


class YahooConnector(Connector):
    name = "yahoo"

    def fetch(
        self,
        symbol: str = "",
        date_from: str = "",
        date_to: str = "",
        bar_size: str = "1 day",
        what_to_show: str = "ADJUSTED_LAST",
        **kwargs,
    ) -> int:
        """Upsert daily bars for one Yahoo symbol (e.g. ``XEQT.TO``, ``GOOG``).

        ``what_to_show="ADJUSTED_LAST"`` (default) stores dividend/split-adjusted bars —
        what the long-term backtest prefers; pass ``"TRADES"`` for raw prices.
        """
        yahoo_symbol = symbol.strip().upper()
        if not yahoo_symbol:
            raise ValueError("symbol is required")
        if what_to_show not in ("ADJUSTED_LAST", "TRADES"):
            raise ValueError("what_to_show must be ADJUSTED_LAST or TRADES")

        start: date | None = _date_from_ymd(date_from) if date_from else None
        if start is None:
            with get_session() as session:
                instrument = _get_instrument(session, yahoo_symbol)
                if instrument:
                    start = _next_missing_date(
                        session=session,
                        instrument=instrument,
                        bar_size=bar_size,
                        source=self.name,
                        what_to_show=what_to_show,
                    )

        if start and start > datetime.now(tz=UTC).date():
            return 0  # already current — Yahoo rejects a start date in the future

        end: date | None = None
        if date_to:
            inclusive_end = _date_from_ymd(date_to)
            if start and start > inclusive_end:
                return 0
            end = inclusive_end + timedelta(days=1)  # yfinance's `end` is exclusive

        _throttle()
        frame = _download_within_timeout(
            yahoo_symbol,
            start,
            end,
            auto_adjust=what_to_show == "ADJUSTED_LAST",
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )

        with get_session() as session:
            instrument = _get_or_create_instrument(session, yahoo_symbol)
            count = 0
            for values in _bar_values(frame):
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
