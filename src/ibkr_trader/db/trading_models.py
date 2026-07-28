"""Trading-side tables: predictions, orders, executions, backtests, strategy state.

**These never leave this repo's Postgres** (docs/plans/active/data-lake.md guardrails): orders
and executions are the tax/audit trail, and strategy snapshots are live risk state. When Phase 2
extracts the lake package, this module stays behind in ``ibkr_trader``.

Depends on :mod:`ibkr_trader.db.lake_models` for ``instruments`` (trading → lake is the only
allowed direction).
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
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ibkr_trader.db.base import Base, JsonVariant, SqliteFriendlyBigInt

# Imported for its side effect: ``instruments``/``backtest_runs`` foreign keys below resolve
# against Base.metadata, so the lake tables must be registered before this module maps.
from ibkr_trader.db.lake_models import Instrument as Instrument  # noqa: F401


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        Index("ix_predictions_instrument_ts", "instrument_id", "ts"),
        UniqueConstraint("backtest_run_id", "instrument_id", "ts"),
    )

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    model_name: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(32), default="0")
    # Null for deployed/forward predictions. OOS research predictions point at the exact
    # backtest run whose fitted fold models produced them, so they cannot be mistaken for
    # in-sample values or mixed across validation invocations.
    backtest_run_id: Mapped[int | None] = mapped_column(ForeignKey("backtest_runs.id"))
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

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict | None] = mapped_column(JSON)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict | None] = mapped_column(JSON)  # sharpe, max_dd, cagr, ...
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrategySnapshot(Base):
    """Forward-only target weights recorded before their returns are known."""

    __tablename__ = "strategy_snapshots"
    __table_args__ = (UniqueConstraint("strategy", "ts"),)

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(32))
    feature_set_version: Mapped[str] = mapped_column(String(16))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    weights: Mapped[dict] = mapped_column(JsonVariant)
    params: Mapped[dict] = mapped_column(JsonVariant)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
