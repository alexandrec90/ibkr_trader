"""CLI tests: universe parsing, argument validation, output formatters, and the DB-backed
`backtest compare` command. Hermetic — no network, no Postgres (compare runs against a
monkeypatched in-memory SQLite session)."""

import re
from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
import typer
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from typer.testing import CliRunner

from ibkr_trader import cli
from ibkr_trader.db.models import BacktestRun, Base

runner = CliRunner()


def _all_output(result) -> str:
    return result.output + result.stderr


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _plain(text: str) -> str:
    """Strip ANSI escape codes — typer's rich error panels style option names mid-string."""
    return _ANSI.sub("", text)


# --- _read_universe -------------------------------------------------------------------


def test_read_universe_symbols_override_file(tmp_path):
    universe_file = tmp_path / "u.txt"
    universe_file.write_text("msft\n")
    assert cli._read_universe(str(universe_file), "aapl, ry.to ,") == ["AAPL", "RY.TO"]


def test_read_universe_reads_file_one_per_line(tmp_path):
    universe_file = tmp_path / "u.txt"
    universe_file.write_text("aapl\n\n xiu.to \n")
    assert cli._read_universe(str(universe_file), "") == ["AAPL", "XIU.TO"]


def test_read_universe_missing_file_is_bad_parameter(tmp_path):
    with pytest.raises(typer.BadParameter, match="cannot read"):
        cli._read_universe(str(tmp_path / "nope.txt"), "")


def test_read_universe_empty_file_is_bad_parameter(tmp_path):
    universe_file = tmp_path / "u.txt"
    universe_file.write_text("\n\n")
    with pytest.raises(typer.BadParameter, match="empty universe"):
        cli._read_universe(str(universe_file), "")


# --- argument validation / stub exit codes --------------------------------------------


def test_help_lists_command_groups():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for group in ("ingest", "backtest", "train", "snapshot"):
        assert group in result.output


def test_ingest_prices_unknown_source_is_refused():
    result = runner.invoke(cli.app, ["ingest", "prices", "AAPL", "--source", "bogus"])
    assert result.exit_code == 2
    assert "unknown source" in _all_output(result)


def test_backtest_run_unknown_strategy_is_refused():
    result = runner.invoke(cli.app, ["backtest", "run", "--strategy", "nope", "--symbols", "AAPL"])
    assert result.exit_code == 2
    assert "unknown allocator" in _all_output(result)


@pytest.mark.parametrize(
    "args",
    [
        ["backtest", "run", "--symbols", "AAPL"],
        ["backtest", "oos", "--end", "2025-01-01", "--symbols", "AAPL"],
        ["train", "run", "--end", "2025-01-01", "--symbols", "AAPL"],
    ],
)
def test_min_history_days_must_be_positive(args):
    result = runner.invoke(cli.app, [*args, "--min-history-days", "0"])
    assert result.exit_code == 2
    assert "range x>=1" in _all_output(result)


def test_backtest_run_flows_history_floor_into_config(monkeypatch):
    from ibkr_trader.backtest import engine as engine_mod

    seen = {}

    def fake_run(self, strategy, universe, start, end, *, config, persist):
        seen["floor"] = config.eligibility.min_history_days
        return SimpleNamespace()

    monkeypatch.setattr(engine_mod.BacktestEngine, "run", fake_run)
    monkeypatch.setattr(cli, "_print_backtest_result", lambda result, account: None)
    result = runner.invoke(
        cli.app,
        ["backtest", "run", "--symbols", "AAPL", "--min-history-days", "63"],
    )
    assert result.exit_code == 0
    assert seen["floor"] == 63


def test_backtest_oos_flows_history_floor_into_config(monkeypatch):
    from ibkr_trader.backtest import oos as oos_mod
    from ibkr_trader.db import session as session_mod

    seen = {}

    @contextmanager
    def fake_session():
        yield object()

    def fake_oos(session, universe, start, end, **kwargs):
        seen["floor"] = kwargs["config"].eligibility.min_history_days
        return SimpleNamespace(
            n_folds=1,
            eval_start=date(2023, 1, 31),
            test_end=date(2023, 6, 30),
            results=[],
        )

    monkeypatch.setattr(session_mod, "get_session", fake_session)
    monkeypatch.setattr(oos_mod, "run_oos_backtest", fake_oos)
    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "oos",
            "--end",
            "2025-01-01",
            "--symbols",
            "AAPL",
            "--min-history-days",
            "63",
        ],
    )
    assert result.exit_code == 0
    assert seen["floor"] == 63


def test_train_run_flows_history_floor_into_limits(monkeypatch, tmp_path):
    from ibkr_trader.db import session as session_mod
    from ibkr_trader.signals import train as train_mod

    seen = {}

    @contextmanager
    def fake_session():
        yield object()

    def fake_train(session, universe, start, end, **kwargs):
        seen["floor"] = kwargs["limits"].min_history_days
        return SimpleNamespace(metadata={}, artifact_dir=tmp_path / "v-test")

    monkeypatch.setattr(session_mod, "get_session", fake_session)
    monkeypatch.setattr(train_mod, "train_from_db", fake_train)
    monkeypatch.setattr(cli, "_print_train_summary", lambda metadata: None)
    result = runner.invoke(
        cli.app,
        [
            "train",
            "run",
            "--end",
            "2025-01-01",
            "--symbols",
            "AAPL",
            "--min-history-days",
            "63",
        ],
    )
    assert result.exit_code == 0
    assert seen["floor"] == 63


def test_train_report_without_artifact_exits_nonzero(tmp_path):
    result = runner.invoke(cli.app, ["train", "report", "--models-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "no trained model found" in _all_output(result)


def test_stub_commands_exit_nonzero():
    """Skeleton commands must fail loudly, not pretend success."""
    for args in (["ibkr-check"],):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 1, args


def test_serve_starts_the_scheduler(monkeypatch):
    """serve delegates to the scheduler (patched here so it doesn't actually block)."""
    started = {}

    def fake_serve():
        started["called"] = True

    monkeypatch.setattr("ibkr_trader.scheduler.serve", fake_serve)
    result = runner.invoke(cli.app, ["serve"])
    assert result.exit_code == 0
    assert started.get("called") is True


def test_ingest_finnhub_news_reports_error_cleanly(monkeypatch):
    """Missing key must exit 1 with a readable message, not a traceback."""
    from ibkr_trader.ingestion.news import finnhub_news

    def boom(self, **kwargs):
        raise RuntimeError("FINNHUB_KEY is not set (see .env.example)")

    monkeypatch.setattr(finnhub_news.FinnhubNewsConnector, "fetch", boom)
    result = runner.invoke(cli.app, ["ingest", "finnhub-news", "AAPL"])
    assert result.exit_code == 1
    assert "FINNHUB_KEY" in result.output


def test_ingest_finnhub_news_reports_count(monkeypatch):
    from ibkr_trader.ingestion.news import finnhub_news

    monkeypatch.setattr(finnhub_news.FinnhubNewsConnector, "fetch", lambda self, **kw: 7)
    result = runner.invoke(cli.app, ["ingest", "finnhub-news", "AAPL"])
    assert result.exit_code == 0
    assert "upserted 7 articles" in result.output


def test_ingest_finnhub_backfill_delegates_to_scheduler_helper(monkeypatch):
    import ibkr_trader.scheduler as scheduler

    seen = {}

    def fake_backfill(universe_file, **kwargs):
        seen["file"] = universe_file
        seen["kwargs"] = kwargs
        return 9

    monkeypatch.setattr(scheduler, "backfill_finnhub_news", fake_backfill)
    result = runner.invoke(
        cli.app,
        ["ingest", "finnhub-backfill", "--days", "120", "--max-requests", "50"],
    )
    assert result.exit_code == 0
    assert "upserted 9 articles" in result.output
    assert seen["file"] == "tickers.txt"
    assert seen["kwargs"]["backfill_days"] == 120
    assert seen["kwargs"]["max_requests"] == 50


def test_ingest_finnhub_backfill_reports_error_cleanly(monkeypatch):
    import ibkr_trader.scheduler as scheduler

    def boom(universe_file, **kwargs):
        raise RuntimeError("FINNHUB_KEY is not set (see .env.example)")

    monkeypatch.setattr(scheduler, "backfill_finnhub_news", boom)
    result = runner.invoke(cli.app, ["ingest", "finnhub-backfill"])
    assert result.exit_code == 1
    assert "FINNHUB_KEY" in result.output


def test_score_sentiment_command_reports_counts(monkeypatch):
    import ibkr_trader.scheduler as scheduler

    monkeypatch.setattr(
        scheduler,
        "run_sentiment_scoring",
        lambda: {"news_articles": 5, "social_posts": 2},
    )
    result = runner.invoke(cli.app, ["score-sentiment"])
    assert result.exit_code == 0
    assert "news_articles" in result.output


def test_ingest_finnhub_news_batch_uses_universe_file(monkeypatch):
    import ibkr_trader.scheduler as scheduler

    seen = {}

    def fake_poll(universe_file, spacing_seconds, **kw):
        seen["file"] = universe_file
        seen["spacing"] = spacing_seconds
        seen["date_from"] = kw.get("date_from")
        return 42

    monkeypatch.setattr(scheduler, "poll_finnhub_news", fake_poll)
    result = runner.invoke(
        cli.app,
        [
            "ingest",
            "finnhub-news",
            "--universe-file",
            "tickers.txt",
            "--spacing-seconds",
            "0",
            "--date-from",
            "2025-07-16",
        ],
    )
    assert result.exit_code == 0
    assert "upserted 42 articles" in result.output
    assert seen == {"file": "tickers.txt", "spacing": 0.0, "date_from": "2025-07-16"}


def test_ingest_news_reports_error_cleanly(monkeypatch):
    """Missing key must exit 1 with a readable message, not a traceback."""
    from ibkr_trader.ingestion.news import newsapi

    def boom(self, **kwargs):
        raise RuntimeError("NEWSAPI_KEY is not set (see .env.example)")

    monkeypatch.setattr(newsapi.NewsApiConnector, "fetch", boom)
    result = runner.invoke(cli.app, ["ingest", "news", "--query", "Nvidia"])
    assert result.exit_code == 1
    assert "NEWSAPI_KEY" in result.output


def test_ingest_news_flows_options_into_fetch(monkeypatch):
    from ibkr_trader.ingestion.news import newsapi

    seen = {}

    def fake_fetch(self, **kwargs):
        seen.update(kwargs)
        return 5

    monkeypatch.setattr(newsapi.NewsApiConnector, "fetch", fake_fetch)
    result = runner.invoke(
        cli.app,
        [
            "ingest",
            "news",
            "--query",
            "Nvidia",
            "--symbol",
            "NVDA",
            "--date-from",
            "2026-06-20",
            "--date-to",
            "2026-07-15",
        ],
    )
    assert result.exit_code == 0
    assert "upserted 5 articles" in result.output
    assert seen == {
        "query": "Nvidia",
        "symbol": "NVDA",
        "date_from": "2026-06-20",
        "date_to": "2026-07-15",
    }


def test_ingest_news_empty_query_exits_nonzero(monkeypatch):
    from ibkr_trader.ingestion.news import newsapi

    def boom(self, **kwargs):
        raise ValueError("query is required")

    monkeypatch.setattr(newsapi.NewsApiConnector, "fetch", boom)
    result = runner.invoke(cli.app, ["ingest", "news"])
    assert result.exit_code == 1
    assert "query is required" in result.output


def test_ingest_news_batch_uses_mapping_file(monkeypatch, tmp_path):
    import ibkr_trader.scheduler as scheduler

    mapping = tmp_path / "news.txt"
    mapping.write_text("AAPL,Apple Inc\nNVDA,Nvidia\n", encoding="utf-8")
    seen = {}

    def fake_poll(pairs, refresh_after_hours=0.0, max_requests=0):
        seen.update(pairs=pairs, refresh=refresh_after_hours, budget=max_requests)
        return 11

    monkeypatch.setattr(scheduler, "poll_newsapi_pairs", fake_poll)
    result = runner.invoke(cli.app, ["ingest", "news", "--mapping-file", str(mapping)])
    assert result.exit_code == 0
    assert "upserted 11 articles" in result.output
    assert seen["pairs"] == [("AAPL", "Apple Inc"), ("NVDA", "Nvidia")]
    assert seen["refresh"] == 12.0  # freshness skip on by default
    assert seen["budget"] == 90  # free tier is 100/day — leave headroom


def test_ingest_news_malformed_mapping_is_bad_parameter(tmp_path):
    mapping = tmp_path / "news.txt"
    mapping.write_text("AAPL Apple\n", encoding="utf-8")  # missing comma
    result = runner.invoke(cli.app, ["ingest", "news", "--mapping-file", str(mapping)])
    assert result.exit_code != 0
    assert "TICKER,search term" in _plain(result.output)


def test_ingest_prices_batch_is_alpha_vantage_only():
    result = runner.invoke(
        cli.app, ["ingest", "prices", "--universe-file", "tickers-av.txt", "--source", "fmp"]
    )
    assert result.exit_code == 2
    assert "alpha_vantage-only" in _plain(_all_output(result))


def test_ingest_prices_batch_flows_into_fetch_universe(monkeypatch, tmp_path):
    from ibkr_trader.ingestion.market import alpha_vantage

    tickers = tmp_path / "t.txt"
    tickers.write_text("# comment\nnvda\nmsft\n", encoding="utf-8")
    seen = {}

    def fake_fetch_universe(symbols, refresh_after_days=0.0, max_requests=0):
        seen.update(symbols=symbols, refresh=refresh_after_days, budget=max_requests)
        return 200

    monkeypatch.setattr(alpha_vantage, "fetch_universe", fake_fetch_universe)
    result = runner.invoke(
        cli.app,
        [
            "ingest",
            "prices",
            "--universe-file",
            str(tickers),
            "--source",
            "alpha_vantage",
        ],
    )
    assert result.exit_code == 0
    assert "upserted 200 bars" in result.output
    assert seen["symbols"] == ["NVDA", "MSFT"]
    assert seen["refresh"] == 1.0  # same-day reruns are free by default
    assert seen["budget"] == 20  # free tier is ~25/day — leave headroom


def test_ingest_prices_requires_symbol_or_file():
    result = runner.invoke(cli.app, ["ingest", "prices"])
    assert result.exit_code != 0
    assert "SYMBOL or --universe-file" in _plain(_all_output(result))


def test_ingest_finnhub_news_requires_symbol_or_file():
    result = runner.invoke(cli.app, ["ingest", "finnhub-news"])
    assert result.exit_code != 0
    assert "SYMBOL or --universe-file" in _plain(result.output)


def test_ingest_trends_batch_uses_mapping_file(monkeypatch, tmp_path):
    import ibkr_trader.scheduler as scheduler

    mapping = tmp_path / "m.txt"
    mapping.write_text("AAPL,Apple\nNVDA,Nvidia\n", encoding="utf-8")
    seen = {}

    def fake_poll(pairs, geo="", timeframe="", refresh_after_days=None):
        seen.update(pairs=pairs, geo=geo, timeframe=timeframe, refresh=refresh_after_days)
        return 99

    monkeypatch.setattr(scheduler, "poll_trends_pairs", fake_poll)
    result = runner.invoke(cli.app, ["ingest", "trends", "--mapping-file", str(mapping)])
    assert result.exit_code == 0
    assert "upserted 99 trend points" in result.output
    assert seen["pairs"] == [("AAPL", "Apple"), ("NVDA", "Nvidia")]
    assert seen["timeframe"] == "today 5-y"  # batch default = 5y weekly backfill
    assert seen["refresh"] == 14.0  # freshness skip on by default


def test_ingest_trends_malformed_mapping_is_bad_parameter(tmp_path):
    mapping = tmp_path / "m.txt"
    mapping.write_text("AAPL Apple\n", encoding="utf-8")  # missing comma
    result = runner.invoke(cli.app, ["ingest", "trends", "--mapping-file", str(mapping)])
    assert result.exit_code != 0
    assert "TICKER,search term" in _plain(result.output)


# --- backtest compare (in-memory DB) ---------------------------------------------------


def _make_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _add_run(session: Session, strategy: str, sharpe: float) -> None:
    session.add(
        BacktestRun(
            strategy=strategy,
            params={
                "model_version": "3",
                "horizon": "12m",
                "universe": {"survivorship": "curated-current"},
            },
            start=datetime(2021, 8, 1, tzinfo=UTC),
            end=datetime(2025, 1, 1, tzinfo=UTC),
            metrics={"sharpe": sharpe, "max_drawdown": -0.15, "cagr": 0.11, "n_days": 850},
            created_at=datetime(2025, 1, 2, tzinfo=UTC),
        )
    )


def _patch_session(monkeypatch, session: Session) -> None:
    @contextmanager
    def fake_get_session():
        yield session

    monkeypatch.setattr("ibkr_trader.db.session.get_session", fake_get_session)


def test_backtest_compare_empty_db(monkeypatch):
    _patch_session(monkeypatch, _make_session())
    result = runner.invoke(cli.app, ["backtest", "compare"])
    assert result.exit_code == 0
    assert "no backtest runs found" in result.output


def test_backtest_compare_renders_leaderboard(monkeypatch):
    session = _make_session()
    _add_run(session, "ml_lt", sharpe=1.234)
    _add_run(session, "momentum_lt", sharpe=0.5)
    session.commit()
    _patch_session(monkeypatch, session)

    result = runner.invoke(cli.app, ["backtest", "compare"])

    assert result.exit_code == 0
    assert "ranked by sharpe (desc), 2 run(s)" in result.output
    assert "1.234" in result.output  # metric cells use 3 decimal places
    assert result.output.index("ml_lt") < result.output.index("momentum_lt")
    assert "curated-current universes are survivorship-biased" in result.output
    assert "Compare strategies only on the identical universe" in result.output


# --- formatters -------------------------------------------------------------------------


def _fake_result(metrics: dict) -> SimpleNamespace:
    return SimpleNamespace(
        strategy="ml_lt",
        start=datetime(2021, 8, 1, tzinfo=UTC),
        end=datetime(2025, 1, 1, tzinfo=UTC),
        params={
            "benchmark": "XEQT",
            "universe": {"survivorship": "curated-current"},
        },
        metrics=metrics,
        trades=40,
        costs_cad=123.0,
        fx_cost_cad=45.0,
        tax_cad=67.0,
    )


def test_print_backtest_result_formats_key_figures(capsys):
    result = _fake_result(
        {
            "start_value_cad": 100_000.0,
            "end_value_cad": 131_000.0,
            "total_return": 0.31,
            "benchmark_end_value_cad": 120_000.0,
            "benchmark_total_return": 0.20,
            "excess_return": 0.11,
            "cagr": 0.083,
            "sharpe": 1.1,
            "sortino": 1.5,
            "max_drawdown": -0.12,
            "trades_per_year": 12.5,
        }
    )
    cli._print_backtest_result(result, "tfsa")
    out = capsys.readouterr().out
    assert "$100,000 → $131,000 CAD (+31.0%)" in out
    assert "Buy-and-hold XEQT" in out
    assert "excess +11.0%" in out
    assert "40 trades (12.5/yr)" in out
    assert "WARNING: curated-current universe is survivorship-biased" in out
    assert "absolute returns are upper bounds" in out


def test_print_backtest_result_omits_benchmark_when_absent(capsys):
    cli._print_backtest_result(_fake_result({"total_return": 0.1}), "rrsp")
    out = capsys.readouterr().out
    assert "Buy-and-hold" not in out
    assert "account=rrsp" in out


def test_print_train_summary_survives_minimal_metadata(capsys):
    cli._print_train_summary({})  # every key is optional — must not raise
    assert capsys.readouterr().out


def test_print_train_summary_prints_folds_and_overall_ic(capsys):
    metadata = {
        "model": "ml_lt",
        "version": "v3",
        "feature_set_version": 2,
        "created_at": "2026-01-15T10:00:00+00:00",
        "label": {"spec": "fwd_12m_excess"},
        "train_window": {
            "n_rows": 1000,
            "n_dates": 48,
            "dataset_dates": ["2015-01-31", "2024-12-31"],
        },
        "universe": {"n_symbols": 25, "sha256_16": "abcd1234"},
        "validation": {
            "scheme": "walk-forward",
            "purge_months": 12,
            "overall_ic": {
                "lgbm": {"mean": 0.05, "std": 0.1, "n_dates": 12},
                "ridge": {"mean": 0.07, "std": 0.08, "n_dates": 12},
            },
            "folds": [
                {
                    "fold_id": 1,
                    "train": ["2015-01-31", "2019-12-31"],
                    "test": ["2021-01-31", "2021-06-30"],
                    "ic": {
                        "lgbm": {"mean": 0.04, "std": 0.09, "n_dates": 6},
                        "ridge": None,  # a fold may miss a model — must render "-", not crash
                    },
                }
            ],
        },
        "note": "purged",
    }
    cli._print_train_summary(metadata)
    out = capsys.readouterr().out
    assert "ml_lt v3" in out
    assert "1000 rows" in out
    assert "overall ridge: +0.070 ±0.080 (12)" in out
