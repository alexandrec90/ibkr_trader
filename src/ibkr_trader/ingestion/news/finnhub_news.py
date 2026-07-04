"""Finnhub company-news connector.

Free tier: ~60 calls/min. Docs: https://finnhub.io/docs/api/company-news
Finnhub also exposes a news-sentiment endpoint worth evaluating later.
"""

import httpx

from ibkr_trader.config import get_settings
from ibkr_trader.ingestion.base import Connector

BASE_URL = "https://finnhub.io/api/v1"


class FinnhubNewsConnector(Connector):
    name = "finnhub"

    def fetch(self, symbol: str = "", date_from: str = "", date_to: str = "", **kwargs) -> int:
        settings = get_settings()
        if not settings.finnhub_key:
            raise RuntimeError("FINNHUB_KEY is not set (see .env.example)")

        # TODO(skeleton): GET /company-news?symbol=&from=&to=, map into NewsArticle rows
        # (external_id = str(item["id"])), upsert. Untested request shape:
        response = httpx.get(
            f"{BASE_URL}/company-news",
            params={"symbol": symbol, "from": date_from, "to": date_to},
            headers={"X-Finnhub-Token": settings.finnhub_key},
            timeout=30,
        )
        response.raise_for_status()
        raise NotImplementedError("persistence not implemented yet")
