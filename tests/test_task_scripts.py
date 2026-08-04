"""Tests for the non-trivial helper scripts behind the VS Code tasks.

These scripts used to live in `.vscode/`, which is editor configuration and not a
script home. Eight Python files were in there, and `task_artifact_runner.py` was
load-bearing far outside the editor: `scripts/lint-all.py` and `scripts/run-tests.py`
both wrap every invocation in it.

They are in `scripts/` now, with the naming split devkit uses: kebab-case for
entrypoints, snake_case for anything imported as a module (`ingest_fmp_tickers`, which
`aggregate-tickers.py` imports as a sibling).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts"


def load_script(name: str) -> ModuleType:
    path = SCRIPT_DIR / name
    module_name = f"_test_vscode_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Every path the SHARED workspace tasks dispatch to, via devkit's `devkit_project.py`.
# This list replaces a check that parsed `.vscode/tasks.json` and asserted each
# `${workspaceFolder}\scripts\…` reference resolved. That file is gone: all five IBKR
# tasks were hoisted to `alex-projects.code-workspace`, because a task defined in this
# repo is rendered once per WORKTREE — `ibkr_trader` and `ibkr_trader-b` are two folders
# in one multi-root workspace, so each of them appeared twice in the quick-pick.
#
# The reference list is written out rather than parsed because the workspace file lives
# ABOVE this repo and is not in it: a test that read it would pass locally and skip (or
# fail) in CI, where there is no workspace. So the invariant is stated from this side —
# these are the paths this repo promises to keep, and devkit's `--check` reports the
# other direction. A rename that misses one turns a one-click action into a
# missing-script error, which is exactly what the old check existed to prevent.
CONTRACT_ENTRYPOINTS = (
    "run-tests.py",  # Test: Run Suite
    "lint-all.py",  # Lint: Everything / Lint: Changed Files
    "vnc-viewer.py",  # IBKR: Open Gateway VNC Viewer
    "ingest-task.py",  # Ingest: Run Source
    "snapshot-monthly.py",  # Snapshot: Run Monthly
    "backtest-task.py",  # Backtest: Run / Backtest: OOS
    "db-revision.py",  # DB: New Migration (Autogenerate)
    # Not dispatched directly, but every one of the above wraps it, so a rename here
    # breaks all of them at once and none of them at import time.
    "task-artifact-runner.py",
)


@pytest.mark.parametrize("name", CONTRACT_ENTRYPOINTS)
def test_every_dispatched_entrypoint_exists(name):
    """A workspace task pointing at a moved script fails only when someone clicks it."""
    assert (SCRIPT_DIR / name).is_file(), f"scripts/{name} is dispatched by a workspace task"


def test_this_repo_ships_no_project_level_tasks():
    """The five IBKR tasks live in the workspace block now, taking the checkout as a
    picker. A `.vscode/tasks.json` here would reintroduce the per-worktree duplicate."""
    assert not (REPO_ROOT / ".vscode" / "tasks.json").exists(), (
        "tasks belong in alex-projects.code-workspace, scoped with devkit_project.py's "
        "`projects=` field, not in a per-worktree file"
    )


def test_no_python_lives_under_dot_vscode():
    """`.vscode/` is editor configuration, not a script home.

    Eight scripts were in there, and the one that mattered most was invisible from the
    outside: `scripts/lint-all.py` and `scripts/run-tests.py` — the paths the SHARED
    workspace tasks call — both wrapped every invocation in `.vscode/
    task_artifact_runner.py`.
    """
    stray = sorted(p.name for p in (REPO_ROOT / ".vscode").glob("*.py"))
    assert stray == [], f"scripts belong in scripts/, not .vscode/: {stray}"


def test_docker_prune_never_requests_volume_deletion(monkeypatch):
    script = load_script("docker-prune.py")
    commands = []

    def fake_step(_label, command):
        commands.append(command)
        return 0

    monkeypatch.setattr(script, "step", fake_step)
    monkeypatch.setattr(script, "assert_volumes_survived", lambda: None)

    script.prune()

    assert commands == [
        ["docker", "compose", "down"],
        ["docker", "system", "prune", "-af"],
        ["docker", "builder", "prune", "-af"],
    ]
    assert all("--volumes" not in command for command in commands)
    assert all(command[:3] != ["docker", "volume", "prune"] for command in commands)


def test_docker_prune_stops_if_protected_volume_is_missing(monkeypatch, capsys):
    script = load_script("docker-prune.py")
    monkeypatch.setattr(script, "run", lambda _command: (0, "some_other_volume"))

    with pytest.raises(SystemExit, match="2"):
        script.assert_volumes_survived()

    assert "protected volume(s) missing" in capsys.readouterr().out


def test_reorder_todo_moves_complete_items_and_is_idempotent():
    script = load_script("reorder-todo.py")
    source = (
        "# Work\n\n"
        "- [x] completed first\n"
        "  continuation stays attached\n"
        "- [ ] active item\n\n"
        "## ✅ Done\n\n"
        "- [X] completed second\n"
    )

    result = script.reorder(source)

    assert result == (
        "# Work\n\n"
        "- [ ] active item\n\n"
        "## ✅ Done\n\n"
        "- [x] completed first\n"
        "  continuation stays attached\n"
        "- [X] completed second\n"
    )
    assert script.reorder(result) == result


def test_ingest_ticker_helper_continues_after_a_symbol_failure(monkeypatch, tmp_path, capsys):
    script = load_script("ingest_fmp_tickers.py")
    ticker_file = tmp_path / "tickers.txt"
    ticker_file.write_text("# comment\nAAPL\n\nMSFT\n", encoding="utf-8")
    connector = SimpleNamespace()

    def fetch(*, symbol):
        if symbol == "AAPL":
            raise RuntimeError("provider unavailable")
        return 12

    connector.fetch = fetch
    monkeypatch.setattr(
        script,
        "parse_args",
        lambda: argparse.Namespace(tickers="tickers.txt", source="fmp"),
    )
    monkeypatch.setattr(script, "make_connector", lambda _source: connector)
    monkeypatch.setattr(script.pathlib.Path, "resolve", lambda _self: tmp_path / "scripts" / "x")

    assert script.main() == 1
    output = capsys.readouterr().out
    assert "AAPL: failed: RuntimeError: provider unavailable" in output
    assert "MSFT: upserted 12 bars" in output
    assert "failed_tickers: AAPL" in output


def test_vnc_viewer_removes_auth_file_after_viewer_exits(monkeypatch, tmp_path):
    script = load_script("vnc-viewer.py")
    auth_file = tmp_path / "auth"
    auth_file.write_bytes(b"secret")
    commands = []
    monkeypatch.setattr(script, "fetch_auth_file", lambda: auth_file)
    monkeypatch.setattr(script.sys, "argv", ["vnc-viewer.py", "viewer.exe"])

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(script.subprocess, "run", fake_run)

    assert script.main() == 7
    assert commands == [
        [
            "viewer.exe",
            "-SecurityTypes",
            "VncAuth",
            "-PasswordFile",
            str(auth_file),
            script.VNC_HOST,
        ]
    ]
    assert not auth_file.exists()


def test_backtest_run_passes_every_selected_window_through():
    script = load_script("backtest-task.py")
    args = script.parse_args(
        [
            "run",
            "--strategy",
            "ml_lt_ridge",
            "--account",
            "tfsa",
            "--universe-file",
            "tickers-etfs.txt",
            "--start",
            "2008-06-02",
            "--eval-start",
            "2010-01-04",
            "--end",
            "2030-01-01",
        ]
    )

    assert script.cli_args(args) == [
        "run",
        "--strategy",
        "ml_lt_ridge",
        "--account",
        "tfsa",
        "--universe-file",
        "tickers-etfs.txt",
        "--start",
        "2008-06-02",
        "--eval-start",
        "2010-01-04",
        "--end",
        "2030-01-01",
    ]


def test_backtest_oos_windows_are_fixed_and_not_selectable():
    """The warm-up and simulation starts are the whole comparability guarantee.

    They were literals in a tasks.json args array; if they ever become options, an OOS
    run stops being comparable to the previously recorded one and nothing says so.
    """
    script = load_script("backtest-task.py")
    args = script.parse_args(["oos", "--account", "rrsp", "--end", "2026-07-01"])
    emitted = script.cli_args(args)

    assert emitted[emitted.index("--start") + 1] == script.OOS_START
    assert emitted[emitted.index("--sim-start") + 1] == script.OOS_SIM_START
    with pytest.raises(SystemExit):
        script.parse_args(
            ["oos", "--account", "rrsp", "--end", "2026-07-01", "--start", "2020-01-01"]
        )


def test_backtest_modes_write_separate_artifacts():
    """A shared artifact would let an OOS run overwrite the evidence from the run that
    prompted it."""
    script = load_script("backtest-task.py")
    run = script.build_argv(script.parse_args(["oos", "--account", "tfsa", "--end", "2026-07-01"]))
    assert run[run.index("--artifact") + 1] == "backtest-oos"
    assert script.ARTIFACTS["run"] != script.ARTIFACTS["oos"]


def test_backtest_reaches_the_cli_as_a_module_not_the_exe_shim():
    """`task-artifact-runner.py` prepends its own `sys.executable`, so the command it
    runs has to be a module command; the old tasks named `.venv\\Scripts\\ibkr-trader.exe`
    directly, which the artifact wrapper cannot invoke."""
    script = load_script("backtest-task.py")
    argv = script.build_argv(script.parse_args(["oos", "--account", "tfsa", "--end", "2026-07-01"]))

    assert argv[1].endswith("task-artifact-runner.py")
    assert argv[argv.index("--") + 1 :][:2] == ["-m", "ibkr_trader.cli"]
    assert not any(part.endswith("ibkr-trader.exe") for part in argv)


def test_backtest_falls_back_to_the_running_interpreter_without_a_venv(tmp_path):
    """VS Code launches tasks with its own PATH, so the venv is looked up explicitly --
    but the script must still run from an activated shell and from CI."""
    script = load_script("backtest-task.py")
    assert script.python_exe(tmp_path) == sys.executable

    scripts_dir = tmp_path / ".venv" / "Scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "python.exe").write_text("")
    assert script.python_exe(tmp_path) == str(scripts_dir / "python.exe")


def test_db_revision_passes_the_message_as_its_own_argv_element():
    """Never through a shell. A message with a quote or a `;` in it is free text from a
    VS Code prompt, and as one argv element it cannot be reinterpreted."""
    script = load_script("db-revision.py")
    argv = script.build_argv("add index; DROP TABLE orders")

    assert argv[-2:] == ["-m", "add index; DROP TABLE orders"]
    assert argv[1:5] == ["-m", "alembic", "revision", "--autogenerate"]


def test_db_revision_never_applies_the_migration():
    """It writes a file and stops. `upgrade` is a separate, deliberate act — an
    autogenerated revision has to be read first."""
    script = load_script("db-revision.py")
    assert "upgrade" not in script.build_argv("whatever")


def test_db_revision_prefers_the_environment_database_url():
    script = load_script("db-revision.py")
    assert script.database_url({"DATABASE_URL": "postgresql://real/url"}) == "postgresql://real/url"
    assert script.database_url({}) == script.DEFAULT_DATABASE_URL
    # 5432 is taken by another local Postgres on this machine.
    assert "5433" in script.DEFAULT_DATABASE_URL


def test_db_revision_reports_the_generated_file_repo_relative():
    """The point of printing the path is that someone opens it."""
    script = load_script("db-revision.py")
    out = [
        "INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.",
        "  Generating C:/Users/x/ibkr_trader/migrations/versions/a1b2_add_index.py ...  done",
    ]
    assert script.created_paths(out) == ["migrations/versions/a1b2_add_index.py"]


def test_db_revision_writes_a_recovery_hint_on_failure(monkeypatch, tmp_path):
    """A stopped `db` is the most likely failure and the least obvious from alembic's
    own error, which is a raw connection refusal."""
    script = load_script("db-revision.py")
    artifact = tmp_path / "db-revision.log"
    monkeypatch.setattr(script, "ARTIFACT", artifact)
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        script.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=1, stdout="", stderr="connection to server failed\n"
        ),
    )

    assert script.main(["-m", "x"]) == 1
    log = artifact.read_text(encoding="utf-8")
    assert "docker compose up -d db" in log
    assert "connection to server failed" in log


def test_db_revision_clears_the_artifact_on_success(monkeypatch, tmp_path):
    """A stale artifact from a previous failure would misdirect the next agent."""
    script = load_script("db-revision.py")
    artifact = tmp_path / "db-revision.log"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("old failure", encoding="utf-8")
    monkeypatch.setattr(script, "ARTIFACT", artifact)
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        script.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0, stdout="Generating /app/migrations/versions/x_y.py ... done\n", stderr=""
        ),
    )

    assert script.main(["-m", "x"]) == 0
    assert artifact.read_text(encoding="utf-8") == ""


def test_task_artifact_runner_records_child_failure(monkeypatch, tmp_path):
    script = load_script("task-artifact-runner.py")
    monkeypatch.setattr(
        script,
        "parse_args",
        lambda: argparse.Namespace(artifact="example", command=["-m", "fake_command"]),
    )
    monkeypatch.setattr(script.pathlib.Path, "resolve", lambda _self: tmp_path / "scripts" / "x")
    monkeypatch.setattr(
        script.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=9, stdout="child output\n", stderr="child error\n"
        ),
    )

    assert script.main() == 9
    log = (tmp_path / "artifacts" / "tasks" / "example.log").read_text(encoding="utf-8")
    assert "exit_code: 9" in log
    assert "===== stdout =====\nchild output" in log
    assert "===== stderr =====\nchild error" in log
