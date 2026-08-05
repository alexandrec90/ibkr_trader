"""CLI tests: universe parsing, argument validation, output formatters, and the DB-backed
`backtest compare` command. Hermetic — no network, no Postgres (compare runs against a
monkeypatched in-memory SQLite session)."""

import re
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
import typer
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from typer import rich_utils
from typer.testing import CliRunner

from ibkr_trader import cli
from ibkr_trader.db.models import BacktestRun, Base

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_rich_panels(monkeypatch):
    """Pin a wide console so rich never wraps error messages mid-substring.

    At the default captured width (80) the wrap point depends on the tmp_path length, so
    substring asserts like "TICKER,search term" would pass or fail by path-length luck.
    """
    monkeypatch.setattr(rich_utils, "MAX_WIDTH", 500)


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


def test_backtest_run_flows_eval_start_into_config(monkeypatch):
    from datetime import date

    from ibkr_trader.backtest import engine as engine_mod

    seen = {}

    def fake_run(self, strategy, universe, start, end, *, config, persist):
        seen["eval_start"] = config.eval_start
        return SimpleNamespace()

    monkeypatch.setattr(engine_mod.BacktestEngine, "run", fake_run)
    monkeypatch.setattr(cli, "_print_backtest_result", lambda result, account: None)
    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "run",
            "--symbols",
            "AAPL",
            "--start",
            "2008-06-01",
            "--eval-start",
            "2010-01-01",
        ],
    )
    assert result.exit_code == 0
    assert seen["eval_start"] == date(2010, 1, 1)


def test_backtest_run_eval_start_default_is_none(monkeypatch):
    from ibkr_trader.backtest import engine as engine_mod

    seen = {}

    def fake_run(self, strategy, universe, start, end, *, config, persist):
        seen["eval_start"] = config.eval_start
        return SimpleNamespace()

    monkeypatch.setattr(engine_mod.BacktestEngine, "run", fake_run)
    monkeypatch.setattr(cli, "_print_backtest_result", lambda result, account: None)
    result = runner.invoke(cli.app, ["backtest", "run", "--symbols", "AAPL"])
    assert result.exit_code == 0
    assert seen["eval_start"] is None


@pytest.mark.parametrize(
    "args",
    [
        ["--eval-start", "not-a-date"],
        ["--start", "2010-01-01", "--eval-start", "2010-01-01"],  # needs warm-up bars before it
        ["--start", "2015-01-01", "--eval-start", "2012-01-01"],
    ],
)
def test_backtest_run_bad_eval_start_is_refused(args):
    result = runner.invoke(cli.app, ["backtest", "run", "--symbols", "AAPL", *args])
    assert result.exit_code != 0
    assert "--eval-start" in _plain(_all_output(result))


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


def test_factor_report_cli_prints_summary_and_passes_run_id(monkeypatch):
    import pandas as pd

    from ibkr_trader.backtest import factor_report as report_mod
    from ibkr_trader.db import session as session_mod

    seen = {}

    @contextmanager
    def fake_session():
        yield object()

    def fake_report(session, run_id, **kwargs):
        seen["run_id"] = run_id
        return {
            "metadata": {"run_id": run_id, "strategy": "ml_lt_ridge_oos"},
            "diagnostics": {"clean_rows": 10, "input_rows": 12},
            "mean_ic": pd.DataFrame({"mean_ic": [0.05]}, index=["21D"]),
            "quantile_returns": pd.DataFrame({"21D": [0.01]}, index=["Q5"]),
            "quantile_diagnostics": pd.DataFrame(
                {"q5_minus_q1": [0.02], "monotonic_ish": [True]}, index=["21D"]
            ),
            "turnover": pd.DataFrame({"mean": [0.25]}, index=["1M"]),
            "artifacts": [],
        }

    monkeypatch.setattr(session_mod, "get_session", fake_session)
    monkeypatch.setattr(report_mod, "generate_factor_report", fake_report)

    result = runner.invoke(cli.app, ["backtest", "factor-report", "--run-id", "42"])

    assert result.exit_code == 0
    assert seen["run_id"] == 42
    assert "OOS factor report" in result.output
    assert "mean rank IC" in result.output


def test_train_run_flows_history_floor_into_limits(monkeypatch, tmp_path):
    from ibkr_trader.db import session as session_mod
    from ibkr_trader.signals import train as train_mod

    seen = {}

    @contextmanager
    def fake_session():
        yield object()

    def fake_train(session, universe, start, end, **kwargs):
        seen["floor"] = kwargs["limits"].min_history_days
        seen["search"] = kwargs["search"]
        seen["n_trials"] = kwargs["n_trials"]
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
            "--search",
            "grid",
            "--n-trials",
            "9",
        ],
    )
    assert result.exit_code == 0
    assert seen["floor"] == 63
    assert seen["search"] == "grid"
    assert seen["n_trials"] == 9


def test_train_run_can_project_artifact_to_local_mlflow(monkeypatch, tmp_path):
    from ibkr_trader.db import session as session_mod
    from ibkr_trader.signals import tracking as tracking_mod
    from ibkr_trader.signals import train as train_mod

    seen = {}

    @contextmanager
    def fake_session():
        yield object()

    artifact_dir = tmp_path / "models" / "ml_lt" / "v1"

    def fake_train(*args, **kwargs):
        return SimpleNamespace(metadata={}, artifact_dir=artifact_dir)

    def fake_track(path, tracking_dir):
        seen["artifact_dir"] = path
        seen["tracking_dir"] = tracking_dir
        return SimpleNamespace(run_id="run-123", tracking_uri=tracking_dir.resolve().as_uri())

    monkeypatch.setattr(session_mod, "get_session", fake_session)
    monkeypatch.setattr(train_mod, "train_from_db", fake_train)
    monkeypatch.setattr(tracking_mod, "log_training_run", fake_track)
    monkeypatch.setattr(cli, "_print_train_summary", lambda metadata: None)
    mlflow_dir = tmp_path / "tracking"
    result = runner.invoke(
        cli.app,
        [
            "train",
            "run",
            "--end",
            "2025-01-01",
            "--symbols",
            "AAPL",
            "--track-mlflow",
            "--mlflow-dir",
            str(mlflow_dir),
        ],
    )

    assert result.exit_code == 0
    assert seen == {"artifact_dir": artifact_dir, "tracking_dir": mlflow_dir}
    assert "MLflow: run run-123" in result.output
    assert mlflow_dir.resolve().as_uri() in result.output


def test_train_run_rejects_unknown_search():
    result = runner.invoke(
        cli.app,
        ["train", "run", "--end", "2025-01-01", "--search", "random"],
    )
    assert result.exit_code == 2
    assert "optuna" in _plain(_all_output(result))
    assert "grid" in _plain(_all_output(result))


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
    from data_lake.ingestion.news import finnhub_news

    def boom(self, **kwargs):
        raise RuntimeError("FINNHUB_KEY is not set (see .env.example)")

    monkeypatch.setattr(finnhub_news.FinnhubNewsConnector, "fetch", boom)
    result = runner.invoke(cli.app, ["ingest", "finnhub-news", "AAPL"])
    assert result.exit_code == 1
    assert "FINNHUB_KEY" in result.output


def test_ingest_finnhub_news_reports_count(monkeypatch):
    from data_lake.ingestion.news import finnhub_news

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
    from data_lake.ingestion.news import newsapi

    def boom(self, **kwargs):
        raise RuntimeError("NEWSAPI_KEY is not set (see .env.example)")

    monkeypatch.setattr(newsapi.NewsApiConnector, "fetch", boom)
    result = runner.invoke(cli.app, ["ingest", "news", "--query", "Nvidia"])
    assert result.exit_code == 1
    assert "NEWSAPI_KEY" in result.output


def test_ingest_news_flows_options_into_fetch(monkeypatch):
    from data_lake.ingestion.news import newsapi

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
    from data_lake.ingestion.news import newsapi

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
    from data_lake.ingestion.market import alpha_vantage

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


def _seed_check_data_bar(session: Session, close: float) -> None:
    from ibkr_trader.db.models import Instrument, PriceBar

    instrument = Instrument(symbol="AAA", exchange="TSX", currency="CAD")
    session.add(instrument)
    session.flush()
    session.add(
        PriceBar(
            instrument_id=instrument.id,
            ts=datetime.now(UTC) - timedelta(days=1),
            bar_size="1 day",
            source="yahoo",
            what_to_show="ADJUSTED_LAST",
            open=10.0,
            high=11.0,
            low=9.0,
            close=close,
            volume=100.0,
        )
    )
    session.commit()


def test_check_data_clean_sqlite_db_exits_zero(monkeypatch):
    session = _make_session()
    _seed_check_data_bar(session, 10.0)
    _patch_session(monkeypatch, session)

    result = runner.invoke(cli.app, ["check-data", "--symbols", "AAA"])

    assert result.exit_code == 0
    assert "AAA: ok (1 rows)" in result.output
    assert "OK: 1 recent price rows passed" in result.output


def test_check_data_bad_sqlite_row_exits_nonzero_and_identifies_row(monkeypatch):
    session = _make_session()
    _seed_check_data_bar(session, -1.0)
    _patch_session(monkeypatch, session)

    result = runner.invoke(cli.app, ["check-data", "--symbols", "AAA"])
    output = _all_output(result)

    assert result.exit_code == 1
    assert "AAA: data-quality violations" in output
    assert "row 0: close failed close_positive" in output
    assert "value=-1.0" in output


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
        "lgbm_search": {
            "method": "optuna",
            "duration_seconds": 12.34,
            "completed_count": 17,
            "pruned_count": 3,
        },
        "note": "purged",
    }
    cli._print_train_summary(metadata)
    out = capsys.readouterr().out
    assert "ml_lt v3" in out
    assert "1000 rows" in out
    assert "overall ridge: +0.070 ±0.080 (12)" in out
    assert "parameter search: optuna · 12.3s · 17 completed / 3 pruned" in out


# --- archive commands (local backend in tmp_path + in-memory DB) ------------------------


def _patch_archive_settings(monkeypatch, tmp_path) -> None:
    """Point the archive at a temp directory, through the real wiring.

    The archive lives in the ``data_lake`` package now and reads whatever settings object the
    CLI's root callback handed ``data_lake.configure``. So patching *this repo's*
    ``get_settings`` is both the honest seam and a test of ``lake.configure_lake`` itself —
    patching something inside the package would bypass the wiring these tests exist to cover.
    """
    _patch_settings(monkeypatch, archive_backend="local", archive_local_dir=str(tmp_path))


def _patch_settings(monkeypatch, **overrides) -> None:
    from ibkr_trader.config import Settings

    settings = Settings(_env_file=None, **overrides)
    monkeypatch.setattr("ibkr_trader.config.get_settings", lambda: settings)


def test_archive_commands_refuse_unconfigured_backend(monkeypatch):
    _patch_settings(monkeypatch, archive_backend="none")
    result = runner.invoke(cli.app, ["archive", "status"])
    assert result.exit_code == 1
    assert "no archive backend configured" in _all_output(result)


def test_archive_status_empty(monkeypatch, tmp_path):
    _patch_archive_settings(monkeypatch, tmp_path)
    result = runner.invoke(cli.app, ["archive", "status"])
    assert result.exit_code == 0
    assert "archive is empty" in result.output


def test_archive_catalog_empty_then_populated_and_rebuild(monkeypatch, tmp_path):
    pytest.importorskip("pyarrow")
    from ibkr_trader.db.models import Instrument, PriceBar

    _patch_archive_settings(monkeypatch, tmp_path)

    empty = runner.invoke(cli.app, ["archive", "catalog"])
    assert empty.exit_code == 0
    assert "catalog is empty" in empty.output

    session = _make_session()
    session.add(Instrument(id=1, symbol="AAPL", exchange="SMART", currency="USD"))
    session.add(
        PriceBar(
            instrument_id=1,
            ts=datetime(2020, 1, 15, 14, 30, tzinfo=UTC),
            bar_size="1 min",
            source="ibkr",
            what_to_show="TRADES",
            open=9.0,
            high=11.0,
            low=8.0,
            close=10.0,
            volume=100.0,
        )
    )
    session.commit()
    _patch_session(monkeypatch, session)
    assert runner.invoke(cli.app, ["archive", "bars", "--older-than-days", "365"]).exit_code == 0

    # status ignores the catalog manifest; catalog summarizes the dataset
    status = runner.invoke(cli.app, ["archive", "status"])
    assert "1 object(s)" in status.output

    listed = runner.invoke(cli.app, ["archive", "catalog"])
    assert listed.exit_code == 0
    assert "price_bars: 1 rows" in listed.output
    assert "key=symbol+exchange+currency+ts+bar_size+source+what_to_show" in listed.output

    rebuilt = runner.invoke(cli.app, ["archive", "catalog", "--rebuild"])
    assert rebuilt.exit_code == 0
    assert "price_bars: 1 rows" in rebuilt.output


def test_archive_bars_falls_back_to_the_configured_window(monkeypatch, tmp_path):
    """Omitting --older-than-days must use the same setting the `serve` job reads, so a manual
    drain and the scheduled run cannot apply different hot/cold boundaries."""
    pytest.importorskip("pyarrow")
    from ibkr_trader.db.models import Instrument, PriceBar

    session = _make_session()
    session.add(Instrument(id=1, symbol="AAPL", exchange="SMART", currency="USD"))
    # 120 days old: inside a 365-day window (would be left alone), outside a 90-day one.
    session.add(
        PriceBar(
            instrument_id=1,
            ts=datetime.now(UTC) - timedelta(days=120),
            bar_size="1 min",
            source="ibkr",
            what_to_show="TRADES",
            open=9.0,
            high=11.0,
            low=8.0,
            close=10.0,
            volume=100.0,
        )
    )
    session.commit()
    _patch_session(monkeypatch, session)
    _patch_settings(
        monkeypatch,
        archive_backend="local",
        archive_local_dir=str(tmp_path),
        archive_bars_older_than_days=90,
    )

    result = runner.invoke(cli.app, ["archive", "bars"])
    assert result.exit_code == 0
    assert "archived 1 bars → deleted 1 local rows" in result.output


def test_archive_bars_window_default_is_ninety_days():
    """The knob that actually bounds local disk. Raising it silently would undo the offload."""
    from ibkr_trader.config import Settings

    assert Settings(_env_file=None).archive_bars_older_than_days == 90


def test_archive_raw_falls_back_to_the_configured_grace(monkeypatch, tmp_path):
    """Omitting --min-age-days uses ARCHIVE_RAW_MIN_AGE_DAYS; a payload inside the grace
    period stays put."""
    pytest.importorskip("pyarrow")
    from ibkr_trader.db.models import NewsArticle

    session = _make_session()
    session.add(
        NewsArticle(
            source="finnhub",
            external_id="fresh-1",
            title="t",
            url="https://example.com/1",
            published_at=datetime.now(UTC) - timedelta(days=1),
            fetched_at=datetime.now(UTC) - timedelta(days=1),
            sentiment=0.5,
            raw={"a": 1},
        )
    )
    session.commit()
    _patch_session(monkeypatch, session)
    _patch_settings(
        monkeypatch,
        archive_backend="local",
        archive_local_dir=str(tmp_path),
        archive_raw_min_age_days=30,
    )

    result = runner.invoke(cli.app, ["archive", "raw"])
    assert result.exit_code == 0
    assert "archived 0 payloads" in result.output


def test_archive_bars_roundtrip_via_cli(monkeypatch, tmp_path):
    pytest.importorskip("pyarrow")
    from ibkr_trader.db.models import Instrument, PriceBar

    session = _make_session()
    session.add(Instrument(id=1, symbol="AAPL", exchange="SMART", currency="USD"))
    session.add(
        PriceBar(
            instrument_id=1,
            ts=datetime(2020, 1, 15, 14, 30, tzinfo=UTC),
            bar_size="1 min",
            source="ibkr",
            what_to_show="TRADES",
            open=9.0,
            high=11.0,
            low=8.0,
            close=10.0,
            volume=100.0,
        )
    )
    session.commit()
    _patch_session(monkeypatch, session)
    _patch_archive_settings(monkeypatch, tmp_path)

    result = runner.invoke(cli.app, ["archive", "bars", "--older-than-days", "365"])
    assert result.exit_code == 0
    assert "archived 1 bars → deleted 1 local rows" in result.output
    assert (tmp_path / "price_bars/bar_size=1_min/2020-01.parquet").is_file()

    status = runner.invoke(cli.app, ["archive", "status"])
    assert status.exit_code == 0
    assert "1 object(s)" in status.output

    restore = runner.invoke(
        cli.app, ["archive", "restore-bars", "--start", "2020-01-01", "--end", "2020-12-31"]
    )
    assert restore.exit_code == 0
    assert "restored 1 bars" in restore.output


def test_archive_raw_roundtrip_via_cli(monkeypatch, tmp_path):
    pytest.importorskip("pyarrow")
    from ibkr_trader.db.models import NewsArticle

    session = _make_session()
    session.add(
        NewsArticle(
            source="finnhub",
            external_id="n1",
            published_at=datetime(2020, 3, 2, tzinfo=UTC),
            title="t",
            sentiment=0.5,
            raw={"category": "company"},
            fetched_at=datetime(2020, 3, 2, tzinfo=UTC),
        )
    )
    session.commit()
    _patch_session(monkeypatch, session)
    _patch_archive_settings(monkeypatch, tmp_path)

    result = runner.invoke(cli.app, ["archive", "raw"])
    assert result.exit_code == 0
    assert "archived 1 payloads → NULLed 1 local" in result.output

    restore = runner.invoke(
        cli.app, ["archive", "restore-raw", "--start", "2020-03-01", "--end", "2020-03-31"]
    )
    assert restore.exit_code == 0
    assert "news_articles: refilled 1 payloads" in restore.output


def test_archive_restore_bars_rejects_bad_date(monkeypatch, tmp_path):
    _patch_archive_settings(monkeypatch, tmp_path)
    result = runner.invoke(
        cli.app, ["archive", "restore-bars", "--start", "not-a-date", "--end", "2020-12-31"]
    )
    assert result.exit_code != 0
    assert "--start must be YYYY-MM-DD" in _plain(_all_output(result))


# --- sentiment model management --------------------------------------------------------


class _FakeFinbertScorer:
    model_name = "finbert"

    def score_many(self, texts, *, batch_size):
        return [0.4] * len(texts)


def _add_sentiment_articles(session: Session, count: int) -> None:
    from ibkr_trader.db.models import NewsArticle

    now = datetime.now(UTC)
    session.add_all(
        [
            NewsArticle(
                source="finnhub",
                external_id=f"sentiment-{index}",
                published_at=now,
                title=f"headline {index}",
                sentiment=0.1,
                sentiment_model="vader",
                fetched_at=now,
            )
            for index in range(count)
        ]
    )
    session.commit()


def test_sentiment_download_cli_invokes_explicit_download(monkeypatch):
    from ibkr_trader.signals import finbert

    called = []
    monkeypatch.setattr(finbert, "download_model", lambda: called.append(True))
    result = runner.invoke(cli.app, ["sentiment", "download"])

    assert result.exit_code == 0
    assert "FinBERT model cached and ready" in result.output
    assert called == [True]


def test_sentiment_rescore_cli_rejects_unknown_model():
    result = runner.invoke(cli.app, ["sentiment", "rescore", "--model", "unknown"])
    assert result.exit_code == 2
    assert "must be vader or finbert" in _plain(_all_output(result))


def test_sentiment_rescore_cli_is_idempotent(monkeypatch):
    from ibkr_trader.db.models import NewsArticle
    from ibkr_trader.signals import sentiment as sentiment_module

    session = _make_session()
    _add_sentiment_articles(session, 2)
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(sentiment_module, "scorer_for_model", lambda model: _FakeFinbertScorer())

    first = runner.invoke(cli.app, ["sentiment", "rescore", "--model", "finbert"])
    second = runner.invoke(cli.app, ["sentiment", "rescore", "--model", "finbert"])

    assert first.exit_code == 0
    assert "rescored 2 rows with finbert" in first.output
    assert second.exit_code == 0
    assert "rescored 0 rows with finbert" in second.output
    assert session.query(NewsArticle).filter_by(sentiment_model="finbert").count() == 2


def test_sentiment_rescore_cli_respects_global_limit(monkeypatch):
    from ibkr_trader.db.models import NewsArticle
    from ibkr_trader.signals import sentiment as sentiment_module

    session = _make_session()
    _add_sentiment_articles(session, 3)
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(sentiment_module, "scorer_for_model", lambda model: _FakeFinbertScorer())

    result = runner.invoke(cli.app, ["sentiment", "rescore", "--model", "finbert", "--limit", "2"])

    assert result.exit_code == 0
    assert "rescored 2 rows with finbert" in result.output
    assert session.query(NewsArticle).filter_by(sentiment_model="finbert").count() == 2
    assert session.query(NewsArticle).filter_by(sentiment_model="vader").count() == 1


def test_ingest_fx_source_yahoo_uses_yahoo_connector(monkeypatch):
    from data_lake.ingestion.market import yahoo_fx

    seen = {}

    def fake_fetch(self, pair="", date_from="", date_to="", **kwargs):
        seen["pair"] = pair
        seen["date_from"] = date_from
        return 4200

    monkeypatch.setattr(yahoo_fx.YahooFxConnector, "fetch", fake_fetch)
    result = runner.invoke(cli.app, ["ingest", "fx", "--source", "yahoo", "--pair", "usdcad"])

    assert result.exit_code == 0
    assert "upserted 4200 FX bars for USDCAD" in result.output
    assert seen == {"pair": "usdcad", "date_from": ""}


def test_ingest_fx_default_source_is_fmp(monkeypatch):
    from data_lake.ingestion.market import fmp_fx

    monkeypatch.setattr(fmp_fx.FmpFxConnector, "fetch", lambda self, **kw: 3)
    result = runner.invoke(cli.app, ["ingest", "fx"])

    assert result.exit_code == 0
    assert "upserted 3 FX bars for USDCAD" in result.output


def test_ingest_fx_unknown_source_is_refused():
    result = runner.invoke(cli.app, ["ingest", "fx", "--source", "boc"])
    assert result.exit_code != 0
    assert "unknown source" in result.output


def test_report_refuses_without_plotly(monkeypatch):
    import importlib.util

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name == "plotly" else real_find_spec(name),
    )
    result = runner.invoke(cli.app, ["report"])
    assert result.exit_code == 1
    assert "uv sync --extra report" in _all_output(result)


def test_report_writes_html_file(monkeypatch, tmp_path):
    pytest.importorskip("plotly")
    _patch_session(monkeypatch, _make_session())
    # build_report_from_db is imported lazily inside the command; patch it at the source module.
    from ibkr_trader.dashboard import report as report_mod

    monkeypatch.setattr(report_mod, "build_report_from_db", lambda session, **kw: "<html>ok</html>")

    out = tmp_path / "r.html"
    result = runner.invoke(cli.app, ["report", "--output", str(out), "--no-open"])

    assert result.exit_code == 0, _all_output(result)
    assert out.read_text(encoding="utf-8") == "<html>ok</html>"
    assert f"wrote {out}" in result.output


# --- health -----------------------------------------------------------------------------
# `serve` swallows job failures by design; this command is the only thing that reports them.


def _health_artifact(tmp_path, jobs):
    from ibkr_trader import job_health

    job_health.reset()
    for name, spec in jobs.items():
        job_health.record_schedule(name, spec["interval"])
        if spec.get("error"):
            job_health.record_failure(name, RuntimeError(spec["error"]))
        elif spec.get("ok"):
            job_health.record_success(name, spec.get("result"))
    target = tmp_path / "health.json"
    job_health.write_artifact(target)
    job_health.reset()
    return target


def test_health_reports_all_green(tmp_path):
    artifact = _health_artifact(
        tmp_path, {"prices": {"interval": 86400, "ok": True, "result": 809}}
    )
    result = runner.invoke(cli.app, ["health", "--artifact", str(artifact)])

    assert result.exit_code == 0, _all_output(result)
    assert "all jobs healthy" in result.output
    assert "809" in result.output
    # Timestamps in the table are rendered to the second so they fit their column instead of
    # running into the next one (a full ISO stamp is 32 chars and used to overflow). The
    # artifact header above the table still prints the full stamp.
    row = next(line for line in result.output.splitlines() if line.startswith("prices"))
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+\d{4}-\d{2}-\d{2} ", row)
    assert "+00:00" not in row


def test_health_exits_nonzero_when_a_job_is_failing(tmp_path):
    artifact = _health_artifact(
        tmp_path,
        {
            "prices": {"interval": 86400, "ok": True},
            "reddit": {"interval": 1800, "error": "REDDIT_CLIENT_ID/SECRET not set"},
        },
    )
    result = runner.invoke(cli.app, ["health", "--artifact", str(artifact)])

    assert result.exit_code == 1
    assert "failing" in result.output
    assert "reddit" in _all_output(result)


def test_health_ignore_excludes_a_known_broken_job_from_the_exit_code(tmp_path):
    """Reddit is awaiting API credentials; it must not mask the health of everything else."""
    artifact = _health_artifact(
        tmp_path,
        {
            "prices": {"interval": 86400, "ok": True},
            "reddit": {"interval": 1800, "error": "REDDIT_CLIENT_ID/SECRET not set"},
        },
    )
    result = runner.invoke(cli.app, ["health", "--artifact", str(artifact), "--ignore", "reddit"])

    assert result.exit_code == 0, _all_output(result)
    assert "(ignored)" in result.output
    # the marker must not run into the next column (it did, at the original width)
    assert "(ignored)never" not in result.output


def test_health_shows_a_job_that_succeeds_without_ingesting(tmp_path):
    """The finnhub-news case: green on every run, two weeks since it last stored a row."""
    from ibkr_trader import job_health

    job_health.reset()
    job_health.record_schedule("finnhub_news", 21600)
    job_health.record_success("finnhub_news", 0)
    artifact = tmp_path / "health.json"
    job_health.write_artifact(artifact)
    job_health.reset()

    result = runner.invoke(cli.app, ["health", "--artifact", str(artifact)])

    assert result.exit_code == 0  # succeeding is not a failure; it is still worth seeing
    assert "last wrote" in result.output
    assert "never" in result.output


def test_health_flags_a_job_that_never_ran(tmp_path):
    artifact = _health_artifact(tmp_path, {"newsapi": {"interval": 43200}})
    result = runner.invoke(cli.app, ["health", "--artifact", str(artifact)])

    assert result.exit_code == 1
    assert "never-run" in result.output


def test_health_reports_a_missing_artifact_clearly(tmp_path):
    result = runner.invoke(cli.app, ["health", "--artifact", str(tmp_path / "absent.json")])

    assert result.exit_code == 1
    assert "cannot read the health artifact" in _all_output(result)


def test_health_json_output_is_machine_readable(tmp_path):
    import json

    artifact = _health_artifact(tmp_path, {"prices": {"interval": 86400, "ok": True}})
    result = runner.invoke(cli.app, ["health", "--artifact", str(artifact), "--json"])

    assert result.exit_code == 0, _all_output(result)
    assert json.loads(result.output)["jobs"]["prices"]["last_success"]
