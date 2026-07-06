"""Smoke tests: the skeleton imports, config gates work, metrics math is sane."""

import os
import subprocess
import sys

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
        "instruments",
        "price_bars",
        "news_articles",
        "social_posts",
        "trend_points",
        "predictions",
        "orders",
        "executions",
        "backtest_runs",
    } <= tables


def test_live_requires_acknowledgement():
    settings = Settings(environment="live", live_trading_acknowledged=False, _env_file=None)
    with pytest.raises(RuntimeError):
        settings.assert_trading_allowed()


def test_paper_trading_allowed():
    Settings(environment="paper", _env_file=None).assert_trading_allowed()


def test_cli_output_survives_cp1252_console():
    """Legacy Windows consoles can't encode the CLI's "→"/"·" — echo must degrade, not crash."""
    result = subprocess.run(
        [sys.executable, "-c", "import ibkr_trader.cli, typer; typer.echo('a\\u2192b\\u00b7c')"],
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.strip() == b"a?b\xb7c"  # "→" unencodable → "?"; "·" exists in cp1252


def test_metrics():
    equity = np.array([100.0, 110.0, 105.0, 120.0])
    assert max_drawdown(equity) == pytest.approx(-5 / 110)
    returns = np.diff(equity) / equity[:-1]
    assert sharpe(returns) != 0
    summary = summarize(equity)
    assert {"sharpe", "max_drawdown", "cagr", "n_days"} <= set(summary)
    assert {"sortino", "calmar", "annual_vol", "total_return"} <= set(summary)
