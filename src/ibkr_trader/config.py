"""Application settings, loaded from environment / .env (pydantic-settings).

Safety model: ENVIRONMENT defaults to "paper". Anything that can transmit a real order
must call `settings.assert_trading_allowed()` first — going live requires BOTH
ENVIRONMENT=live and LIVE_TRADING_ACKNOWLEDGED=true.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from ibkr_trader.accounts import AccountType


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["dev", "paper", "live"] = "paper"
    live_trading_acknowledged: bool = False

    # PostgreSQL
    # host port 5433: the compose file maps the container's 5432 there (5432 is taken locally)
    database_url: str = "postgresql+psycopg://trader:trader@127.0.0.1:5433/ibkr_trader"

    # IBKR
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4004  # gnzsnz ib-gateway docker paper port; native Gateway paper = 4002
    ibkr_client_id: int = 1
    # A paper account is a separate simulated account, not one of the funded accounts below.
    # Keep the old singular name as a temporary fallback for existing private .env files.
    ibkr_paper_account: str = ""  # paper account IDs start with "DU"
    ibkr_account: str = ""  # deprecated: use IBKR_PAPER_ACCOUNT
    # Funded-account references. These are deliberately never selected for execution while
    # the project is paper-only; they document the logical account mapping for future work.
    ibkr_margin_account: str = ""  # non-registered individual margin account
    ibkr_rrsp_account: str = ""
    ibkr_tfsa_account: str = ""
    ibkr_fhsa_account: str = ""
    ibkr_lira_account: str = ""

    # Data-source credentials
    newsapi_key: str = ""
    finnhub_key: str = ""
    alpha_vantage_key: str = ""
    fmp_key: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "ibkr-trader/0.1"

    # Ingestion defaults
    subreddits: list[str] = ["wallstreetbets", "investing", "stocks", "CanadianInvestor"]
    # Google Trends search terms (brand/company names, not tickers). Empty => trends poll no-ops.
    trends_keywords: list[str] = []
    # Preferred over trends_keywords when the file exists: 'TICKER,search term' lines tying
    # each Trends series to a tickers.txt symbol (see trends-keywords.txt).
    trends_mapping_file: str = "trends-keywords.txt"

    # `serve` scheduler cadence — periodic ingestion + raw pruning. All opt-in via `serve`;
    # nothing here runs unless the long-running process is started.
    poll_reddit_minutes: int = 30
    poll_finnhub_news_hours: int = 6
    poll_trends_hours: int = 24
    prune_raw_hours: int = 24
    # Grace on fetched_at before a scored row's raw is dropped; 0 = drop as soon as scored.
    prune_raw_min_age_days: int = 0
    # Universe the finnhub-news poll iterates (one symbol per line).
    news_universe_file: str = "tickers.txt"
    # Spacing between finnhub-news calls in the poll; ~1.1 s keeps us under the free 60 req/min.
    finnhub_request_spacing_seconds: float = 1.1
    # Finnhub history backfill (serve job): walks company news backwards from the oldest stored
    # article to a rolling floor of today - FINNHUB_BACKFILL_DAYS (free tier serves ~1 year).
    # Self-healing: once a symbol's history reaches the floor the job costs zero requests, so a
    # daily cadence is cheap. The per-run request budget defers any remainder to the next run.
    finnhub_backfill_hours: int = 24
    finnhub_backfill_days: int = 365
    finnhub_backfill_chunk_days: int = 30
    finnhub_backfill_max_requests: int = 2000
    # Cadence for VADER-scoring rows where sentiment IS NULL (news + social). Scoring also
    # unlocks raw-payload pruning, so this indirectly bounds disk usage.
    score_sentiment_minutes: int = 60
    # Daily-bar refresh: incremental Yahoo fetch for every yahoo-tracked instrument plus FX
    # pairs from FMP. Runs at startup too, so bars catch up after the machine was off.
    poll_prices_hours: int = 24
    fx_pairs: list[str] = ["USDCAD"]

    # Registered-account long-term strategy defaults.
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
    ml_lt_model_dir: str = "models/ml_lt"  # trained ml_lt artifacts (<vN>/ dirs + latest marker)

    def configured_live_accounts(self) -> dict[AccountType, str]:
        """Return non-empty funded-account references keyed by the strategy account type.

        This is configuration metadata only. Order paths must use
        :meth:`ibkr_execution_account`, which refuses to return these IDs while this project is
        paper-only.
        """
        accounts = {
            AccountType.NONREG: self.ibkr_margin_account.strip(),
            AccountType.RRSP: self.ibkr_rrsp_account.strip(),
            AccountType.TFSA: self.ibkr_tfsa_account.strip(),
            AccountType.FHSA: self.ibkr_fhsa_account.strip(),
            AccountType.LIRA: self.ibkr_lira_account.strip(),
        }
        configured = {account: account_id for account, account_id in accounts.items() if account_id}
        if len(set(configured.values())) != len(configured):
            raise RuntimeError("Each configured IBKR funded account ID must be unique.")
        return configured

    def configured_live_account_for(self, account: AccountType | str) -> str:
        """Resolve a funded-account reference without authorizing it for execution."""
        account_type = AccountType(account)
        try:
            return self.configured_live_accounts()[account_type]
        except KeyError:
            env_name = {
                AccountType.NONREG: "IBKR_MARGIN_ACCOUNT",
                AccountType.RRSP: "IBKR_RRSP_ACCOUNT",
                AccountType.TFSA: "IBKR_TFSA_ACCOUNT",
                AccountType.FHSA: "IBKR_FHSA_ACCOUNT",
                AccountType.LIRA: "IBKR_LIRA_ACCOUNT",
            }[account_type]
            raise RuntimeError(f"{env_name} is not configured.") from None

    def ibkr_execution_account(self, account: AccountType | str) -> str:
        """Return the safe IBKR order target for a logical account type.

        IBKR supplies a separate simulated account. RRSP/TFSA/FHSA tax behavior is modelled by
        this application; selecting one of those logical types must not route a paper order to
        the similarly named funded account.
        """
        AccountType(account)  # validate the caller's logical account value
        self.assert_trading_allowed()
        if self.environment != "paper":
            raise RuntimeError("IBKR live-account routing is disabled; use the paper environment.")

        paper_account = self.ibkr_paper_account.strip()
        legacy_account = self.ibkr_account.strip()
        if paper_account and legacy_account and paper_account != legacy_account:
            raise RuntimeError(
                "IBKR_PAPER_ACCOUNT and deprecated IBKR_ACCOUNT disagree; keep only "
                "IBKR_PAPER_ACCOUNT."
            )
        account_id = paper_account or legacy_account
        if not account_id:
            raise RuntimeError("IBKR_PAPER_ACCOUNT is not configured.")
        if not account_id.upper().startswith("DU"):
            raise RuntimeError(
                "IBKR_PAPER_ACCOUNT must be a simulated account ID starting with DU."
            )
        return account_id

    def assert_trading_allowed(self) -> None:
        """Guard called before any order is transmitted to the broker."""
        if self.environment == "live" and not self.live_trading_acknowledged:
            raise RuntimeError("ENVIRONMENT=live but LIVE_TRADING_ACKNOWLEDGED is not true.")
        if self.environment == "dev":
            raise RuntimeError("Trading is disabled in ENVIRONMENT=dev (use paper).")


@lru_cache
def get_settings() -> Settings:
    return Settings()
