"""SQLAlchemy 2.0 models — the single source of truth for all pipeline data.

The tables sit either side of the data-lake seam (docs/plans/active/data-lake.md), and since
Phase 2 the two halves live in different repositories:

- :mod:`data_lake.db.models` — market/corporate/news/social/feature data, shareable. Owned by
  the `data-lake <https://github.com/alexandrec90/data-lake>`_ package, which this repo consumes.
- :mod:`ibkr_trader.db.trading_models` — predictions, orders, executions, backtests, strategy
  state. **Never** leaves this repo; declared here against the package's ``Base``.

Because both halves map onto one ``Base``, a single ``Base.metadata`` still describes the whole
database — which is what Alembic's ``target_metadata`` needs. This module re-exports everything
so ``from ibkr_trader.db.models import X`` keeps working everywhere. Import it, not the halves:
``data_lake.db.models`` alone does not register the trading tables, so autogenerate against it
would propose dropping them.

Conventions:
- Timestamps are timezone-aware UTC.
- External payloads are preserved in a `raw` JSON column for reprocessing.
- Upsert keys: (source, external_id) for text content; (instrument, ts, bar_size, source,
  what_to_show) for bars.
- Privacy (Québec Law 25): social authors are stored as
  hashes, never usernames.
"""

from data_lake.db.base import Base, JsonVariant, SqliteFriendlyBigInt
from data_lake.db.models import (
    Dividend,
    EarningsEvent,
    Feature,
    FundamentalSnapshot,
    Instrument,
    Listing,
    NewsArticle,
    PriceBar,
    ShareCount,
    SocialPost,
    TrendPoint,
)

from ibkr_trader.db.trading_models import (
    BacktestRun,
    Execution,
    Order,
    OrderSide,
    Prediction,
    StrategySnapshot,
)

#: Tables owned by the shared data-lake package (non-PII, non-account).
#:
#: This is what the lake *declares*, not what this repo materialises. ``listings`` is
#: apt-finder's housing data, which lives in the lake because it is ordinary shared content;
#: it lands in our ``Base.metadata`` simply because importing the package registers the whole
#: schema. :mod:`ibkr_trader.db.adoption` is what keeps it out of our migrations.
LAKE_TABLES = frozenset(
    {
        "dividends",
        "earnings_events",
        "features",
        "fundamental_snapshots",
        "instruments",
        "listings",
        "news_articles",
        "price_bars",
        "share_counts",
        "social_posts",
        "trend_points",
    }
)

#: Tables that stay in this repo's Postgres — audit trail and live risk state.
TRADING_TABLES = frozenset(
    {
        "backtest_runs",
        "executions",
        "orders",
        "predictions",
        "strategy_snapshots",
    }
)

__all__ = [
    "LAKE_TABLES",
    "TRADING_TABLES",
    "BacktestRun",
    "Base",
    "Dividend",
    "EarningsEvent",
    "Execution",
    "Feature",
    "FundamentalSnapshot",
    "Instrument",
    "JsonVariant",
    "Listing",
    "NewsArticle",
    "Order",
    "OrderSide",
    "Prediction",
    "PriceBar",
    "ShareCount",
    "SocialPost",
    "SqliteFriendlyBigInt",
    "StrategySnapshot",
    "TrendPoint",
]
