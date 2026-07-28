"""SQLAlchemy 2.0 models — the single source of truth for all pipeline data.

The tables are defined in two modules either side of the data-lake seam
(docs/plans/active/data-lake.md):

- :mod:`ibkr_trader.db.lake_models` — market/corporate/news/social/feature data, shareable;
  destined for the ``data-lake`` package in Phase 2.
- :mod:`ibkr_trader.db.trading_models` — predictions, orders, executions, backtests, strategy
  state; **never** leaves this repo.

This module re-exports both so ``from ibkr_trader.db.models import X`` keeps working everywhere
(including Alembic's ``target_metadata = Base.metadata``, which needs every table registered).
Import it — not the halves — from application code; ``lake_models`` alone does not register the
trading tables.

Conventions:
- Timestamps are timezone-aware UTC.
- External payloads are preserved in a `raw` JSON column for reprocessing.
- Upsert keys: (source, external_id) for text content; (instrument, ts, bar_size, source,
  what_to_show) for bars.
- Privacy (Québec Law 25): social authors are stored as
  hashes, never usernames.
"""

from ibkr_trader.db.base import Base, JsonVariant, SqliteFriendlyBigInt
from ibkr_trader.db.lake_models import (
    Dividend,
    EarningsEvent,
    Feature,
    FundamentalSnapshot,
    Instrument,
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

#: Tables the shared data lake may hold (non-PII, non-account).
LAKE_TABLES = frozenset(
    {
        "dividends",
        "earnings_events",
        "features",
        "fundamental_snapshots",
        "instruments",
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
