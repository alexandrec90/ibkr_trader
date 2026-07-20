"""Tests for the non-trivial helper scripts used by VS Code tasks."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / ".vscode"


def load_script(name: str) -> ModuleType:
    path = SCRIPT_DIR / name
    module_name = f"_test_vscode_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def git_result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


def test_git_sync_rebases_clean_attached_branch(monkeypatch, capsys):
    script = load_script("git_sync_branch.py")
    results = iter(
        [
            git_result(),
            git_result(stdout="feature/test\n"),
            git_result(),
            git_result(stdout="abc123\n"),
            git_result(),
        ]
    )
    calls = []

    def fake_run_git(*args, capture_output=False):
        calls.append((args, capture_output))
        return next(results)

    monkeypatch.setattr(script, "run_git", fake_run_git)

    assert script.main() == 0
    assert calls == [
        (("status", "--porcelain"), True),
        (("branch", "--show-current"), True),
        (("fetch", "--prune", "origin"), False),
        (("rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"), True),
        (("rebase", "origin/main"), False),
    ]
    assert "Syncing 'feature/test' with origin/main" in capsys.readouterr().out


def test_git_sync_refuses_dirty_worktree_without_fetching(monkeypatch, capsys):
    script = load_script("git_sync_branch.py")
    calls = []

    def fake_run_git(*args, capture_output=False):
        calls.append((args, capture_output))
        return git_result(stdout=" M src/example.py\n")

    monkeypatch.setattr(script, "run_git", fake_run_git)

    assert script.main() == 1
    assert calls == [(("status", "--porcelain"), True)]
    assert "will never stash" in capsys.readouterr().err


def test_git_sync_refuses_detached_head_without_fetching(monkeypatch, capsys):
    script = load_script("git_sync_branch.py")
    results = iter([git_result(), git_result()])
    calls = []

    def fake_run_git(*args, capture_output=False):
        calls.append((args, capture_output))
        return next(results)

    monkeypatch.setattr(script, "run_git", fake_run_git)

    assert script.main() == 1
    assert [call[0][0] for call in calls] == ["status", "branch"]
    assert "HEAD is detached" in capsys.readouterr().err


@pytest.mark.parametrize("failed_step", ["status", "branch", "fetch", "rebase"])
def test_git_sync_propagates_git_failure(monkeypatch, failed_step):
    script = load_script("git_sync_branch.py")

    def fake_run_git(*args, capture_output=False):
        if args[0] == failed_step:
            return git_result(returncode=23, stderr="git failed\n")
        if args[0] == "branch":
            return git_result(stdout="feature/test\n")
        if args[0] == "rev-parse":
            return git_result(stdout="abc123\n")
        return git_result()

    monkeypatch.setattr(script, "run_git", fake_run_git)

    assert script.main() == 23


def test_git_sync_reports_missing_origin_main(monkeypatch, capsys):
    script = load_script("git_sync_branch.py")

    def fake_run_git(*args, capture_output=False):
        if args[0] == "branch":
            return git_result(stdout="feature/test\n")
        if args[0] == "rev-parse":
            return git_result(returncode=1)
        return git_result()

    monkeypatch.setattr(script, "run_git", fake_run_git)

    assert script.main() == 1
    assert "origin/main was not found" in capsys.readouterr().err


def test_git_sync_task_invokes_the_tested_script():
    tasks = json.loads((SCRIPT_DIR / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    task = next(task for task in tasks if task["label"].startswith("git: sync branch"))

    assert task["type"] == "process"
    assert task["command"].endswith(".venv\\Scripts\\python.exe")
    assert task["args"] == ["${workspaceFolder}\\.vscode\\git_sync_branch.py"]


def test_every_vscode_task_script_reference_exists():
    tasks = json.loads((SCRIPT_DIR / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    prefix = "${workspaceFolder}\\.vscode\\"
    references = [
        argument
        for task in tasks
        for argument in [task.get("command", ""), *task.get("args", [])]
        if argument.startswith(prefix) and argument.endswith(".py")
    ]

    assert references
    for reference in references:
        assert (SCRIPT_DIR / reference.removeprefix(prefix)).is_file(), reference


def test_docker_prune_never_requests_volume_deletion(monkeypatch):
    script = load_script("docker_prune.py")
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
    script = load_script("docker_prune.py")
    monkeypatch.setattr(script, "run", lambda _command: (0, "some_other_volume"))

    with pytest.raises(SystemExit, match="2"):
        script.assert_volumes_survived()

    assert "protected volume(s) missing" in capsys.readouterr().out


def test_reorder_todo_moves_complete_items_and_is_idempotent():
    script = load_script("reorder_todo.py")
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


def test_sync_claude_helpers_copy_content_and_respect_exclusions(monkeypatch, tmp_path):
    script = load_script("sync_claude_to_agents.py")
    monkeypatch.setattr(script, "ROOT", tmp_path)
    (tmp_path / "CLAUDE.md").write_text("root rules", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "claude.md").write_text("nested rules", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "CLAUDE.md").write_text("excluded", encoding="utf-8")
    (tmp_path / ".claude" / "rules").mkdir(parents=True)
    (tmp_path / ".claude" / "rules" / "testing.md").write_text("test policy", encoding="utf-8")

    assert script.sync_claude_markdown() == 2
    assert script.sync_claude_config() == 1
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "root rules"
    assert (tmp_path / "nested" / "AGENTS.md").read_text(encoding="utf-8") == "nested rules"
    assert not (tmp_path / ".venv" / "AGENTS.md").exists()
    assert (tmp_path / ".agents" / "rules" / "testing.md").read_text(
        encoding="utf-8"
    ) == "test policy"


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
    monkeypatch.setattr(script.pathlib.Path, "resolve", lambda _self: tmp_path / ".vscode" / "x")

    assert script.main() == 1
    output = capsys.readouterr().out
    assert "AAPL: failed: RuntimeError: provider unavailable" in output
    assert "MSFT: upserted 12 bars" in output
    assert "failed_tickers: AAPL" in output


def test_vnc_viewer_removes_auth_file_after_viewer_exits(monkeypatch, tmp_path):
    script = load_script("vnc_viewer.py")
    auth_file = tmp_path / "auth"
    auth_file.write_bytes(b"secret")
    commands = []
    monkeypatch.setattr(script, "fetch_auth_file", lambda: auth_file)
    monkeypatch.setattr(script.sys, "argv", ["vnc_viewer.py", "viewer.exe"])

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


def test_task_artifact_runner_records_child_failure(monkeypatch, tmp_path):
    script = load_script("task_artifact_runner.py")
    monkeypatch.setattr(
        script,
        "parse_args",
        lambda: argparse.Namespace(artifact="example", command=["-m", "fake_command"]),
    )
    monkeypatch.setattr(script.pathlib.Path, "resolve", lambda _self: tmp_path / ".vscode" / "x")
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
