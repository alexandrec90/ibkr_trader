"""`ibkr-trader questrade check|plan`: wiring, token guards and failure messages.

The Questrade client and the database are replaced at their seams; the planner and the
report run for real.
"""

import json
from contextlib import contextmanager
from datetime import date

import pytest
import typer
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from ibkr_trader import cli
from ibkr_trader.accounts import AccountType
from ibkr_trader.advisory import commands, service, sync, targets
from ibkr_trader.advisory import questrade as qt
from ibkr_trader.advisory.planner import AccountState, PlanConfig, Target
from ibkr_trader.advisory.targets import StrategyTargets, TargetWeight
from ibkr_trader.config import get_settings

runner = CliRunner()
STATE = AccountState(
    number="12345282",
    kind=AccountType.TFSA,
    label="TFSA",
    equity_cad=10_000.0,
    usdcad=1.4,
    available={"CAD": 10_000.0},
)
TARGETS = StrategyTargets("core", date(2026, 10, 1), [TargetWeight("XIC", "CAD", 1.0, True, 0.01)])


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Run in a temp dir whose .env holds the token, as the task's working directory does."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("QUESTRADE_TOKEN=tok-1\n")
    return tmp_path / ".env"


@pytest.fixture
def fake_questrade(monkeypatch):
    connected = []
    monkeypatch.setattr(
        qt.QuestradeClient,
        "connect",
        classmethod(lambda cls, store, token: connected.append(token) or "client"),
    )
    monkeypatch.setattr(sync, "account_states", lambda client: [STATE])
    monkeypatch.setattr(
        sync,
        "resolve_targets",
        lambda client, t: ([Target("XIC.TO", "CAD", 1.0, 40.0, broad_etf=True)], ["note A"]),
    )
    return connected


@pytest.fixture
def fake_db(monkeypatch):
    @contextmanager
    def session():
        yield None

    monkeypatch.setattr("ibkr_trader.db.session.get_session", session)
    monkeypatch.setattr(targets, "latest_targets", lambda s, strategy, today: TARGETS)


def test_check_lists_accounts_and_says_the_token_rotated(env, fake_questrade, capsys):
    commands.questrade_check()
    out = capsys.readouterr().out
    assert "TFSA ...282  equity CAD 10,000" in out
    assert "1 active account(s)" in out
    assert fake_questrade == ["tok-1"]


def test_plan_prints_tickets_and_writes_the_csv(env, fake_questrade, fake_db, tmp_path):
    result = runner.invoke(cli.app, ["questrade", "plan", "--strategy", "core", "--csv", "o.csv"])

    assert result.exit_code == 0, result.output
    assert "ADVISORY ONLY" in result.output
    assert "Buy  XIC.TO" in result.output
    assert "! note A" in result.output
    assert (tmp_path / "o.csv").read_text().count("XIC.TO") == 3  # three rungs
    plan = json.loads((tmp_path / "o.json").read_text())  # what `ibkr-trader ui` shows
    assert plan["strategy"] == "core"
    assert len(plan["accounts"][0]["orders"]) == 3


def test_plan_on_a_stopped_database_says_how_to_start_it(env, fake_questrade, monkeypatch):
    @contextmanager
    def down():
        raise OperationalError("SELECT 1", {}, Exception("refused"))
        yield

    monkeypatch.setattr("ibkr_trader.db.session.get_session", down)
    result = runner.invoke(cli.app, ["questrade", "plan", "--strategy", "core"])
    assert result.exit_code == 1
    assert "docker compose up -d db" in result.output
    assert fake_questrade == []


def test_plan_called_directly_honours_its_limits(env, fake_questrade, fake_db, tmp_path, capsys):
    commands.questrade_plan(
        strategy="core",
        min_order=20_000.0,
        max_position=0.2,
        max_new=3,
        exits=False,
        csv_path=str(tmp_path / "o.csv"),
    )
    assert "(no orders)" in capsys.readouterr().out  # nothing reaches a 20,000 minimum


def test_a_missing_token_fails_with_how_to_get_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["questrade", "check"])
    assert result.exit_code == 1
    assert "New manual authorization" in result.output


def test_a_token_only_in_the_shell_is_refused_before_it_is_spent(
    tmp_path, monkeypatch, fake_questrade
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUESTRADE_TOKEN", "tok-1")
    with pytest.raises(typer.Exit):
        commands.questrade_check()
    assert fake_questrade == []


def test_a_failed_login_exits_cleanly(env, monkeypatch):
    def refuse(cls, store, token):
        raise qt.QuestradeError("login failed (HTTP 400)")

    monkeypatch.setattr(qt.QuestradeClient, "connect", classmethod(refuse))
    result = runner.invoke(cli.app, ["questrade", "check"])
    assert result.exit_code == 1
    assert "login failed (HTTP 400)" in result.output


def test_plan_without_a_snapshot_fails_before_touching_questrade(env, fake_questrade, monkeypatch):
    @contextmanager
    def session():
        yield None

    def no_snapshot(s, strategy, today):
        raise ValueError("no forward snapshot for 'core'")

    monkeypatch.setattr("ibkr_trader.db.session.get_session", session)
    monkeypatch.setattr(targets, "latest_targets", no_snapshot)
    result = runner.invoke(cli.app, ["questrade", "plan", "--strategy", "core"])
    assert result.exit_code == 1
    assert "no forward snapshot" in result.output
    assert fake_questrade == []


def test_service_make_plan_returns_the_plans_and_book_notes(env, fake_questrade, fake_db):
    """The path the UI takes: no typer, a PlanResult back, the token spent once."""
    result = service.make_plan("core", PlanConfig(), date(2026, 10, 18))

    assert result.snapshot_asof == date(2026, 10, 1)
    assert result.notes == ["note A"]  # the book is fresh, so only the resolver's note
    assert len(result.plans[0].orders) == 3
    assert result.title.startswith("Questrade order plan · core (snapshot 2026-10-01)")
    assert fake_questrade == ["tok-1"]


def test_service_connect_raises_plan_error_not_exit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QUESTRADE_TOKEN", raising=False)
    with pytest.raises(service.PlanError, match="QUESTRADE_TOKEN is not set"):
        service.connect(tmp_path / ".env")


def test_plan_requires_a_strategy():
    result = runner.invoke(cli.app, ["questrade", "plan"])
    assert result.exit_code == 2
