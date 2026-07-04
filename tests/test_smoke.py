"""Smoke tests: the skeleton imports, config gates work, metrics math is sane."""

import numpy as np
import pytest

from ibkr_trader.backtest.metrics import max_drawdown, sharpe, summarize
from ibkr_trader.config import Settings
from ibkr_trader.db.models import Base


def test_package_imports():
    import ibkr_trader
    import ibkr_trader.backtest.engine
    import ibkr_trader.cli
    import ibkr_trader.execution.ibkr_broker
    import ibkr_trader.ingestion.market.ibkr_historical
    import ibkr_trader.ingestion.news.newsapi
    import ibkr_trader.ingestion.social.reddit
    import ibkr_trader.signals.features

    assert ibkr_trader.__version__


def test_all_tables_registered():
    tables = set(Base.metadata.tables)
    assert {
        "instruments", "price_bars", "news_articles", "social_posts",
        "trend_points", "predictions", "orders", "executions", "backtest_runs",
    } <= tables


def test_live_requires_acknowledgement():
    settings = Settings(environment="live", live_trading_acknowledged=False, _env_file=None)
    with pytest.raises(RuntimeError):
        settings.assert_trading_allowed()


def test_paper_trading_allowed():
    Settings(environment="paper", _env_file=None).assert_trading_allowed()


def test_metrics():
    equity = np.array([100.0, 110.0, 105.0, 120.0])
    assert max_drawdown(equity) == pytest.approx(-5 / 110)
    returns = np.diff(equity) / equity[:-1]
    assert sharpe(returns) != 0
    summary = summarize(equity)
    assert set(summary) == {"sharpe", "max_drawdown", "cagr", "n_days"}
