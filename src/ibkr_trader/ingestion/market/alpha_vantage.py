"""Alpha Vantage connector — daily(+adjusted) OHLCV. Free tier is tiny (~25 req/day):
use it for slow-moving backfills only. Docs: https://www.alphavantage.co/documentation/
"""

import httpx

from ibkr_trader.config import get_settings
from ibkr_trader.ingestion.base import Connector

BASE_URL = "https://www.alphavantage.co/query"


class AlphaVantageConnector(Connector):
    name = "alpha_vantage"

    def fetch(self, symbol: str = "", **kwargs) -> int:
        settings = get_settings()
        if not settings.alpha_vantage_key:
            raise RuntimeError("ALPHA_VANTAGE_KEY is not set (see .env.example)")

        # TODO(skeleton): function=TIME_SERIES_DAILY_ADJUSTED, outputsize=full → PriceBar rows
        # (bar_size="1 day", source="alpha_vantage", what_to_show="ADJUSTED_LAST").
        # NB: TSX symbols use the "SHOP.TRT" style on Alpha Vantage — map via instruments table.
        response = httpx.get(
            BASE_URL,
            params={
                "function": "TIME_SERIES_DAILY_ADJUSTED",
                "symbol": symbol,
                "outputsize": "compact",
                "apikey": settings.alpha_vantage_key,
            },
            timeout=30,
        )
        response.raise_for_status()
        raise NotImplementedError("persistence not implemented yet")
