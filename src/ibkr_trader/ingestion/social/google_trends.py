"""Google Trends connector via pytrends (UNOFFICIAL — expect breakage and throttling).

pytrends scrapes the Trends site; Google rate-limits aggressively (HTTP 429). Keep request
volume tiny (a handful of keyword batches per day), add long backoff, and treat this source as
best-effort garnish, not a load-bearing input.

The interest value is a 0-100 index **relative to the request window**, so points are only
comparable when the timeframe is held constant — this connector pins a single default
``timeframe`` and stores ``(keyword, geo, ts)``. Change the window and old/new points stop
being comparable.
"""

import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from ibkr_trader.db.models import TrendPoint
from ibkr_trader.db.session import get_session
from ibkr_trader.ingestion.base import Connector

#: Keep points comparable across fetches — do not vary this per call.
DEFAULT_TIMEFRAME = "now 7-d"

#: pytrends is unofficial and rate-limited hard; space calls far apart. Module-level so any
#: future batching shares one budget. Tests set this to 0.
MIN_REQUEST_INTERVAL_SECONDS = 60.0
_last_request_monotonic: float | None = None


def throttle() -> None:
    """Sleep so consecutive Trends requests are at least the min interval apart."""
    global _last_request_monotonic
    now = time.monotonic()
    if _last_request_monotonic is not None:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (now - _last_request_monotonic)
        if wait > 0:
            time.sleep(wait)
    _last_request_monotonic = time.monotonic()


def _trends_client() -> Any:
    """Build a pytrends client. Isolated so tests monkeypatch this and never import pytrends."""
    from pytrends.request import TrendReq

    # tz in minutes; 300 = UTC-5 (Montréal). hl = host language.
    return TrendReq(hl="en-US", tz=300)


class GoogleTrendsConnector(Connector):
    name = "google_trends"

    def fetch(
        self,
        keywords: list[str] | None = None,
        geo: str = "CA",
        timeframe: str = DEFAULT_TIMEFRAME,
        **kwargs,
    ) -> int:
        keywords = [kw.strip() for kw in (keywords or []) if kw.strip()]
        if not keywords:
            raise ValueError("at least one keyword is required")
        # Trends caps a single payload at 5 keywords.
        batch = keywords[:5]

        throttle()
        pytrends = _trends_client()
        pytrends.build_payload(kw_list=batch, timeframe=timeframe, geo=geo)
        frame = pytrends.interest_over_time()

        if frame is None or frame.empty:
            return 0

        count = 0
        with get_session() as session:
            for index, row in frame.iterrows():
                if bool(row.get("isPartial", False)):
                    continue  # provisional bucket — skip so we don't store a moving value
                ts = _to_utc(index)
                for keyword in batch:
                    if keyword not in row:
                        continue
                    interest = float(row[keyword])
                    existing = session.scalar(
                        select(TrendPoint).where(
                            TrendPoint.keyword == keyword,
                            TrendPoint.geo == geo,
                            TrendPoint.ts == ts,
                        )
                    )
                    if existing:
                        existing.interest = interest
                    else:
                        session.add(TrendPoint(keyword=keyword, geo=geo, ts=ts, interest=interest))
                    count += 1
        return count


def _to_utc(index_value: Any) -> datetime:
    """pytrends indexes by naive/tz-aware timestamps; normalise to a UTC datetime."""
    dt = index_value.to_pydatetime() if hasattr(index_value, "to_pydatetime") else index_value
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
