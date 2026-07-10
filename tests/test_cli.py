"""CLI tests: universe parsing, argument validation, output formatters, and the DB-backed
`backtest compare` command. Hermetic — no network, no Postgres (compare runs against a
monkeypatched in-memory SQLite session)."""

from contextlib import contextmanager
from datetime import UTC, datetime
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
    for group in ("ingest", "backtest", "train"):
        assert group in result.output


def test_ingest_prices_unknown_source_is_refused():
    result = runner.invoke(cli.app, ["ingest", "prices", "AAPL", "--source", "bogus"])
    assert result.exit_code == 2
    assert "unknown source" in _all_output(result)


def test_backtest_run_unknown_strategy_is_refused():
    result = runner.invoke(cli.app, ["backtest", "run", "--strategy", "nope", "--symbols", "AAPL"])
    assert result.exit_code == 2
    assert "unknown allocator" in _all_output(result)


def test_train_report_without_artifact_exits_nonzero(tmp_path):
    result = runner.invoke(cli.app, ["train", "report", "--models-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "no trained model found" in _all_output(result)


def test_stub_commands_exit_nonzero():
    """Skeleton commands must fail loudly, not pretend success."""
    for args in (["ibkr-check"], ["serve"]):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 1, args


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
            params={"model_version": "3", "horizon": "12m"},
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


# --- formatters -------------------------------------------------------------------------


def _fake_result(metrics: dict) -> SimpleNamespace:
    return SimpleNamespace(
        strategy="ml_lt",
        start=datetime(2021, 8, 1, tzinfo=UTC),
        end=datetime(2025, 1, 1, tzinfo=UTC),
        params={"benchmark": "XEQT"},
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
