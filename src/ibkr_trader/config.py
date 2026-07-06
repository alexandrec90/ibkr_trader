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
    database_url: str = "postgresql+psycopg://trader:trader@127.0.0.1:5433/ibkr_trader"

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

    # Registered-account long-term strategy defaults (docs/registered-account-strategy.md).
    # These are env-overridable deployment defaults; a backtest run pins its own values into
    # backtest_runs.params. Account type is a per-run choice (rrsp|tfsa|fhsa|lira|nonreg).
    base_currency: str = "CAD"  # everything is reported in CAD; US holdings carry FX risk
    default_account: str = "tfsa"
    annual_trade_budget: int = 100  # per account, buys + sells (hard cap; cost fn does the work)
    rebalance_band: float = 0.05  # only trade a name whose target weight drifts past this
    min_price: float = 5.0  # penny-stock floor for eligibility
    min_avg_dollar_volume: float = 1_000_000.0  # liquidity floor (CAD) for eligibility
    us_dividend_yield_assumption: float = 0.015  # for the withholding-drag approximation
    churn_penalty_bps: float = 10.0  # extra soft cost on turnover (the low-turnover lever)
    fx_conversion_bps: float = 20.0  # CAD<->USD conversion spread when shifting currency mix
    benchmark_symbol: str = "XEQT"  # buy-and-hold comparison

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
