"""Application settings, loaded from environment / .env (pydantic-settings).

Safety model: ENVIRONMENT defaults to "paper". Anything that can transmit a real order
must call `settings.assert_trading_allowed()` first — going live requires BOTH
ENVIRONMENT=live and LIVE_TRADING_ACKNOWLEDGED=true.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["dev", "paper", "live"] = "paper"
    live_trading_acknowledged: bool = False

    # PostgreSQL
    # host port 5433: the compose file maps the container's 5432 there (5432 is taken locally)
    database_url: str = "postgresql+psycopg://trader:trader@localhost:5433/ibkr_trader"

    # IBKR — see docs/ibkr/01-connectivity-and-setup.md for the port table
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4004  # gnzsnz ib-gateway docker paper port; native Gateway paper = 4002
    ibkr_client_id: int = 1
    ibkr_account: str = ""  # paper accounts start with "DU"

    # Data-source credentials (docs/data-sources.md)
    newsapi_key: str = ""
    finnhub_key: str = ""
    alpha_vantage_key: str = ""
    fmp_key: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "ibkr-trader/0.1"

    # Ingestion defaults
    subreddits: list[str] = ["wallstreetbets", "investing", "stocks", "CanadianInvestor"]

    def assert_trading_allowed(self) -> None:
        """Guard called before any order is transmitted to the broker."""
        if self.environment == "live" and not self.live_trading_acknowledged:
            raise RuntimeError(
                "ENVIRONMENT=live but LIVE_TRADING_ACKNOWLEDGED is not true. "
                "Read docs/legal-quebec-canada.md and docs/ibkr/02-paper-trading.md first."
            )
        if self.environment == "dev":
            raise RuntimeError("Trading is disabled in ENVIRONMENT=dev (use paper).")


@lru_cache
def get_settings() -> Settings:
    return Settings()
