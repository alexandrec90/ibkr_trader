"""`ibkr-trader backtest lab`: option validation, the verdict formatter, and a hermetic run
against a synthetic universe (monkeypatched DB loaders, no Postgres, no browser)."""

import math
from contextlib import contextmanager
from datetime import date, timedelta

import pytest
import typer
from typer.testing import CliRunner

from ibkr_trader import cli
from ibkr_trader.accounts import AccountType
from ibkr_trader.backtest import lab
from ibkr_trader.backtest.engine import Series

runner = CliRunner()


def test_lab_settings_defaults_to_config_account_and_settings_costs():
    from ibkr_trader.config import get_settings

    settings, start = cli._lab_settings("", "2012-01-03")
    assert settings.account == AccountType.TFSA  # config default
    assert start == date(2012, 1, 3)
    assert settings.cost_model is not None
    assert settings.cost_model.churn_penalty_bps == get_settings().churn_penalty_bps
    assert settings.base_config.annual_trade_budget == get_settings().annual_trade_budget
    assert settings.base_config.eligibility == lab.LAB_ELIGIBILITY


@pytest.mark.parametrize(
    ("account", "compare"),
    [
        ("tfsa", AccountType.RRSP),  # withheld → contrast with the treaty-exempt RRSP
        ("fhsa", AccountType.RRSP),
        ("rrsp", AccountType.TFSA),  # exempt → contrast with the withheld TFSA
        ("lira", AccountType.TFSA),
        ("nonreg", AccountType.RRSP),  # recoverable, but not a registered account
    ],
)
def test_lab_settings_picks_the_contrasting_withholding_regime(account, compare):
    assert cli._lab_settings(account, "2012-01-03")[0].compare_account == compare


@pytest.mark.parametrize(("account", "start"), [("cash", "2012-01-03"), ("tfsa", "x")])
def test_lab_settings_rejects_bad_values(account, start):
    with pytest.raises(typer.BadParameter):
        cli._lab_settings(account, start)


def test_format_lab_verdict_best_first():
    result = lab.LabResult(
        specs=[
            lab.StrategySpec(lab.REFERENCE, "Couch", "", lab.CouchPotatoAllocator, 12, 0.05),
            lab.StrategySpec("x", "Ex", "", lab.CouchPotatoAllocator, 6, 0.05),
        ],
        windows=[lab.Window("w", date(2020, 1, 1), 1.0)],
        account="tfsa",
        runs=[
            lab.LabRun(lab.REFERENCE, "w", "tfsa", {"cagr": 0.10}, []),
            lab.LabRun("x", "w", "tfsa", {"cagr": 0.13}, []),
        ],
    )
    text = cli._format_lab_verdict(result)
    assert "(TFSA)" in text
    assert text.index("Ex") < text.index("Couch")
    assert "+3.0 pts/yr" in text


def _synthetic_inputs():
    cal = [date(2023, 1, 2) + timedelta(days=i) for i in range(800)]
    symbols = [("XIC", "CAD"), ("SPY", "USD"), ("EFA", "USD"), ("EEM", "USD")]
    symbols += [(f"STK{i}", "CAD") for i in range(8)]
    universe = {}
    for iid, (symbol, currency) in enumerate(symbols, 1):
        closes = [60.0 * math.exp(0.0002 * iid * i) for i in range(len(cal))]
        universe[iid] = Series(
            iid, symbol, currency, "TSX", list(cal), closes, closes, [1_000_000.0] * len(cal)
        )
    return lab.LabInputs(universe)


def _patch_db(monkeypatch, inputs):
    from ibkr_trader.db import session as session_mod
    from ibkr_trader.signals import mood as mood_mod

    @contextmanager
    def fake_session():
        yield object()

    monkeypatch.setattr(session_mod, "get_session", fake_session)
    monkeypatch.setattr(lab, "load_lab_inputs", lambda session, symbols, start, end: inputs)
    monkeypatch.setattr(
        mood_mod, "load_mood_panel", lambda session, model: mood_mod.MoodPanel(daily={})
    )
    monkeypatch.setattr(lab, "LAB_ELIGIBILITY", lab.EligibilityLimits(min_history_days=260))


def test_backtest_lab_is_wired_under_backtest():
    commands = {c.name: c.callback for c in cli.backtest_app.registered_commands}
    assert commands["lab"] is cli.backtest_lab


def test_backtest_lab_writes_the_report(monkeypatch, tmp_path):
    pytest.importorskip("plotly")
    _patch_db(monkeypatch, _synthetic_inputs())
    universe_file = tmp_path / "u.txt"
    universe_file.write_text("XIC\nSPY\n", encoding="utf-8")
    out = tmp_path / "lab.html"
    result = runner.invoke(
        cli.app,
        [
            "backtest",
            "lab",
            "--universe-file",
            str(universe_file),
            "--full-start",
            "2024-03-01",
            "--output",
            str(out),
            "--no-open",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Recency-weighted CAGR edge" in result.output
    assert "Momentum + news mood" in result.output  # --mood is the default
    page = out.read_text(encoding="utf-8")
    assert "Registered-account strategy lab" in page
    assert "No news/social mood data" in page  # the empty panel has no usable start


def test_backtest_lab_without_bars_exits_nonzero(monkeypatch, tmp_path):
    pytest.importorskip("plotly")
    _patch_db(monkeypatch, lab.LabInputs({}))
    universe_file = tmp_path / "u.txt"
    universe_file.write_text("XIC\n", encoding="utf-8")
    result = runner.invoke(
        cli.app,
        ["backtest", "lab", "--universe-file", str(universe_file), "--no-mood", "--no-open"],
    )
    assert result.exit_code == 1
    assert "no daily bars" in result.output + result.stderr
