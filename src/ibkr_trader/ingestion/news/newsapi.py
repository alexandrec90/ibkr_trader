"""NewsAPI (newsapi.org) connector — headline search by keyword/company name.

Free tier: ~100 requests/day, non-commercial, articles delayed 24 h. Docs:
https://newsapi.org/docs/endpoints/everything
"""

import httpx

from ibkr_trader.config import get_settings
from ibkr_trader.ingestion.base import Connector

BASE_URL = "https://newsapi.org/v2"


class NewsApiConnector(Connector):
    name = "newsapi"

    def fetch(self, query: str = "", **kwargs) -> int:
        settings = get_settings()
        if not settings.newsapi_key:
            raise RuntimeError("NEWSAPI_KEY is not set (see .env.example)")

        # TODO(skeleton): page through /everything?q=..., map articles into NewsArticle
        # rows (external_id = stable_hash(url)), upsert via a repository helper, extract
        # tickers later in signals/. Untested stub below shows the request shape only.
        response = httpx.get(
            f"{BASE_URL}/everything",
            params={"q": query, "language": "en", "sortBy": "publishedAt"},
            headers={"X-Api-Key": settings.newsapi_key},
            timeout=30,
        )
        response.raise_for_status()
        raise NotImplementedError("persistence not implemented yet")
