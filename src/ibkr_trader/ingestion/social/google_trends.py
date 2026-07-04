"""Google Trends connector via pytrends (UNOFFICIAL — expect breakage and throttling).

pytrends scrapes the Trends site; Google rate-limits aggressively (HTTP 429). Keep request
volume tiny (a handful of keyword batches per day), add long backoff, and treat this source
as best-effort garnish, not a load-bearing input.
"""

from ibkr_trader.ingestion.base import Connector


class GoogleTrendsConnector(Connector):
    name = "google_trends"

    def fetch(self, keywords: list[str] | None = None, geo: str = "CA", **kwargs) -> int:
        # TODO(skeleton): from pytrends.request import TrendReq
        #   pytrends = TrendReq(hl="en-US", tz=300)  # tz in minutes; 300 = UTC-5 (Montréal)
        #   pytrends.build_payload(kw_list=keywords[:5], timeframe="now 7-d", geo=geo)
        #   df = pytrends.interest_over_time()  -> upsert TrendPoint rows on (keyword, geo, ts)
        # Note: interest is a 0-100 index *relative to the request window* — store the window
        # or re-request consistent windows, otherwise points across fetches aren't comparable.
        raise NotImplementedError("pytrends wiring not implemented yet")
