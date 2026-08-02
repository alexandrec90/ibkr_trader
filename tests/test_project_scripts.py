"""Tests for `scripts/docker-down.py` and `scripts/snapshot-monthly.py`.

Both scripts exist because VS Code tasks were carrying logic that belonged in Python:
`docker-down.py` satisfies the shared "Docker: Stop Stack" contract so the profile
handling stops living in a task string, and `snapshot-monthly.py` replaces a six-task
`dependsOn` chain whose ingest link was a pwsh one-liner doing `$LASTEXITCODE`
arithmetic inside a JSON string.

The point of moving them here is that the logic becomes assertable. These tests do not
run Docker or touch a database — everything worth checking is in the pure planning
functions, which is why the scripts expose them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_script(relpath: str):
    """Import a hyphen-named script from `scripts/`.

    `docker-down.py` is not a legal module name, so it cannot be imported normally.
    The module is registered in `sys.modules` before `exec_module` runs because
    `@dataclass` resolves its string annotations by looking the defining module up by
    name — exec'ing first fails inside `dataclasses` with a traceback pointing at
    CPython internals rather than at the loader.
    """
    path = REPO_ROOT / relpath
    name = path.stem.replace("-", "_")
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {relpath}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


docker_down = load_script("scripts/docker-down.py")
snapshot = load_script("scripts/snapshot-monthly.py")


# --- docker-down ------------------------------------------------------------


def test_teardown_covers_every_profile_the_compose_file_defines():
    """A bare `docker compose down` leaves profiled services running and exits 0.

    `ib-gateway` is under the `ibkr` profile and `app` under `app`
    (docker-compose.yml), so the old task's success meant "the db stopped" while the
    heaviest container — a full Java GUI plus VNC — kept its RAM on a machine
    CLAUDE.md describes as memory-constrained.
    """
    argv = docker_down.compose_argv()
    assert argv[:2] == ["docker", "compose"]
    assert argv[-1] == "down"
    for profile in ("ibkr", "app"):
        assert argv[argv.index(profile) - 1] == "--profile"


def test_teardown_never_destroys_volumes():
    """`pgdata` holds the ingested bar history; re-ingesting it is hours against
    rate-limited APIs. This runs from a one-click task over a project picker, so the
    wrong entry must not be able to cost that."""
    for profiles in ((), ("ibkr", "app")):
        assert not ({"-v", "--volumes"} & set(docker_down.compose_argv(profiles)))


def test_db_only_teardown_leaves_the_profiled_services_alone():
    assert docker_down.compose_argv(()) == ["docker", "compose", "down"]


def test_unexpected_task_arguments_do_not_become_a_usage_error(monkeypatch):
    """devkit's docker-maint.py forwards whatever the shared task passed.

    The action sends nothing today, but a future argument must not turn into argparse
    exiting 2 — which the task would surface as a teardown failure whose body is a
    usage message and whose cause is in another repo.
    """
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(docker_down.subprocess, "run", fake_run)
    assert docker_down.main(["--some-future-flag"]) == 0
    assert seen["cmd"][-1] == "down"


# --- snapshot-monthly: the plan ---------------------------------------------


def test_the_ritual_runs_in_the_order_the_data_requires():
    """Migrations before ingest, prices before aggregation, snapshot last.

    This ordering was previously expressed only as `dependsOn` edges across five
    hidden tasks, so it could not be read in one place or checked at all.
    """
    names = [step.name for step in snapshot.plan()]
    assert names.index("apply-migrations") < names.index("ingest-fmp-prices")
    assert names.index("ingest-yahoo-prices") < names.index("aggregate-tickers")
    assert names[-1] == "snapshot-run"


def test_postgres_is_started_and_then_waited_for():
    """`up -d` returns before the database accepts connections, so the migration step
    that follows it would race. The old task chain had no wait at all."""
    names = [step.name for step in snapshot.plan()]
    assert names[:2] == ["start-postgres", "wait-for-postgres"]


def test_skip_ingest_drops_only_the_ingest_steps():
    names = [step.name for step in snapshot.plan(skip_ingest=True)]
    assert "ingest-fmp-prices" not in names
    assert "aggregate-tickers" not in names
    # The parts that make the snapshot valid at all are not optional.
    assert names[:3] == ["start-postgres", "wait-for-postgres", "apply-migrations"]
    assert names[-1] == "snapshot-run"


def test_both_price_ingests_run_even_if_one_fails():
    """The pwsh one-liner's actual semantics, preserved: run both, then fail if either
    did. They are independent sources and a partial month is still worth having —
    which is why they are the only tolerated failures."""
    assert snapshot.TOLERATED == frozenset({"ingest-fmp-prices", "ingest-yahoo-prices"})


def test_a_failed_migration_is_not_tolerated():
    """Ingesting into a schema that does not match the models corrupts the month
    rather than skipping it."""
    assert "apply-migrations" not in snapshot.TOLERATED
    assert "snapshot-run" not in snapshot.TOLERATED


def test_plan_is_pure_and_runs_nothing(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("plan() must not execute anything")

    monkeypatch.setattr(snapshot.subprocess, "run", explode)
    assert snapshot.plan()


def test_dry_run_prints_the_plan_without_executing(monkeypatch, capsys):
    def explode(*args, **kwargs):
        raise AssertionError("--dry-run must not execute anything")

    monkeypatch.setattr(snapshot.subprocess, "run", explode)
    assert snapshot.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "nothing will run" in out
    assert "snapshot-run" in out


def test_full_is_accepted_as_a_no_op(monkeypatch):
    """The task picker's affirming token. If argparse rejected it, the DEFAULT branch
    of the picker would be the one branch that always failed."""
    monkeypatch.setattr(snapshot.subprocess, "run", lambda *a, **k: None)
    assert snapshot.main(["--full", "--dry-run"]) == 0


# --- snapshot-monthly: the connection string --------------------------------


def test_the_environment_wins_over_the_default():
    """It used to be inlined in five task entries, credentials and all. A machine with
    a real `.env` must not be overridden by the fallback."""
    assert snapshot.database_url({"DATABASE_URL": "postgresql://elsewhere/db"}) == (
        "postgresql://elsewhere/db"
    )


def test_the_default_targets_the_non_standard_host_port():
    """5433, not 5432: another local Postgres owns the default (CLAUDE.md). Getting
    this wrong connects to the wrong database and reports success."""
    assert ":5433/" in snapshot.database_url({})


def test_an_empty_environment_value_falls_back_rather_than_connecting_to_nothing():
    assert snapshot.database_url({"DATABASE_URL": ""}) == snapshot.DEFAULT_DATABASE_URL


# --- snapshot-monthly: the interpreter --------------------------------------


def test_the_venv_interpreter_is_preferred(tmp_path):
    """VS Code launches tasks with its own PATH, not the venv's, so a bare `python`
    resolves to whatever the desktop picked and dies on the first project import."""
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")
    assert snapshot.python_exe(tmp_path) == str(scripts / "python.exe")


def test_it_falls_back_to_the_current_interpreter(tmp_path):
    """Keeps the script runnable from an activated shell and in CI, where there is no
    `.venv/Scripts/python.exe` to find."""
    assert snapshot.python_exe(tmp_path) == sys.executable


# --- the artifact -----------------------------------------------------------


def test_a_clean_run_clears_the_artifact(monkeypatch, tmp_path):
    """Written on success too, empty. A stale artifact sends the next agent chasing a
    failure that is already fixed."""
    artifact = tmp_path / "snapshot-monthly.log"
    monkeypatch.setattr(snapshot, "ARTIFACT", artifact)
    artifact.write_text("# a failure from last month\n", encoding="utf-8")
    snapshot.write_artifact([])
    assert artifact.read_text(encoding="utf-8") == ""


def test_a_failing_step_names_itself_and_its_fix(monkeypatch, tmp_path):
    artifact = tmp_path / "snapshot-monthly.log"
    monkeypatch.setattr(snapshot, "ARTIFACT", artifact)
    snapshot.write_artifact(["# apply-migrations\n# fix: alembic upgrade head\nboom"])
    body = artifact.read_text(encoding="utf-8")
    assert "# source: scripts/snapshot-monthly.py" in body
    assert "# fix:" in body, "the artifact must say how to reproduce the failure"


@pytest.mark.parametrize("step", ["start-postgres", "apply-migrations", "snapshot-run"])
def test_every_step_carries_a_runnable_command(step):
    """A step whose argv is empty would `subprocess.run([])` and raise IndexError deep
    in the loop rather than reporting which step was misconfigured."""
    by_name = {s.name: s for s in snapshot.plan()}
    assert by_name[step].argv, f"{step} has no command"
