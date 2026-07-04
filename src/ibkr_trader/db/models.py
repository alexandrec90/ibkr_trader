"""SQLAlchemy 2.0 models — the single source of truth for all pipeline data.

Conventions:
- Timestamps are timezone-aware UTC.
- External payloads are preserved in a `raw` JSON column for reprocessing.
- Upsert keys: (source, external_id) for text content; (instrument, ts, bar_size, source,
  what_to_show) for bars.
- Privacy (Québec Law 25, see docs/legal-quebec-canada.md): social authors are stored as
  hashes, never usernames.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


SqliteFriendlyBigInt = BigInteger().with_variant(Integer, "sqlite")


class Instrument(Base):
    """Canonical tradable instrument; maps provider-specific symbols to one row."""

    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("symbol", "exchange", "currency"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    exchange: Mapped[str] = mapped_column(String(32), default="SMART")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    sec_type: Mapped[str] = mapped_column(String(8), default="STK")
    ibkr_con_id: Mapped[int | None] = mapped_column(BigInteger)  # cached from qualifyContracts
    name: Mapped[str | None] = mapped_column(String(256))


class PriceBar(Base):
    __tablename__ = "price_bars"
    __table_args__ = (
        UniqueConstraint("instrument_id", "ts", "bar_size", "source", "what_to_show"),
        Index("ix_price_bars_instrument_ts", "instrument_id", "ts"),
    )

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bar_size: Mapped[str] = mapped_column(String(16))  # e.g. "1 day", "1 min"
    source: Mapped[str] = mapped_column(String(32))  # ibkr | alpha_vantage | fmp | finnhub
    what_to_show: Mapped[str] = mapped_column(String(32), default="TRADES")  # or ADJUSTED_LAST…
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("ix_news_published_at", "published_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # newsapi | finnhub
    external_id: Mapped[str] = mapped_column(String(256))  # provider id or URL hash
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    symbols: Mapped[list | None] = mapped_column(JSON)  # extracted tickers
    sentiment: Mapped[float | None] = mapped_column(Float)  # filled by signals stage
    raw: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SocialPost(Base):
    __tablename__ = "social_posts"
    __table_args__ = (
        UniqueConstraint("platform", "external_id"),
        Index("ix_social_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))  # reddit
    channel: Mapped[str] = mapped_column(String(64))  # subreddit name
    external_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    author_hash: Mapped[str | None] = mapped_column(String(64))  # sha256, never the username
    title: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    score: Mapped[int | None] = mapped_column(BigInteger)
    num_comments: Mapped[int | None] = mapped_column(BigInteger)
    symbols: Mapped[list | None] = mapped_column(JSON)
    sentiment: Mapped[float | None] = mapped_column(Float)
    raw: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TrendPoint(Base):
    """Google Trends interest-over-time samples."""

    __tablename__ = "trend_points"
    __table_args__ = (UniqueConstraint("keyword", "geo", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    keyword: Mapped[str] = mapped_column(String(128))
    geo: Mapped[str] = mapped_column(String(8), default="")  # "" = worldwide, "CA" = Canada
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interest: Mapped[float] = mapped_column(Float)  # 0-100 relative index


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (Index("ix_predictions_instrument_ts", "instrument_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    model_name: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(32), default="0")
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # as-of time
    horizon: Mapped[str] = mapped_column(String(16))  # e.g. "1d", "5d"
    score: Mapped[float] = mapped_column(Float)  # signed signal, + = long
    features: Mapped[dict | None] = mapped_column(JSON)  # snapshot for auditability
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderSide(enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class Order(Base):
    """Every order we ever transmit (paper or live) — this is also the tax/audit trail."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    environment: Mapped[str] = mapped_column(String(8))  # paper | live
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide))
    quantity: Mapped[float] = mapped_column(Float)
    order_type: Mapped[str] = mapped_column(String(16), default="MKT")  # MKT | LMT | ...
    limit_price: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="PendingSubmit")
    ibkr_order_id: Mapped[int | None] = mapped_column(BigInteger)
    ibkr_perm_id: Mapped[int | None] = mapped_column(BigInteger)  # stable across sessions
    prediction_id: Mapped[int | None] = mapped_column(ForeignKey("predictions.id"))
    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    avg_fill_price: Mapped[float | None] = mapped_column(Float)
    filled_quantity: Mapped[float | None] = mapped_column(Float)
    raw: Mapped[dict | None] = mapped_column(JSON)


class Execution(Base):
    """Individual fills (IBKR execDetails); several per order are possible."""

    __tablename__ = "executions"
    __table_args__ = (UniqueConstraint("ibkr_exec_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    ibkr_exec_id: Mapped[str] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    commission: Mapped[float | None] = mapped_column(Float)
    raw: Mapped[dict | None] = mapped_column(JSON)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict | None] = mapped_column(JSON)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict | None] = mapped_column(JSON)  # sharpe, max_dd, cagr, ...
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
