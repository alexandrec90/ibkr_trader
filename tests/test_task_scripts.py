"""Tests for the non-trivial helper scripts behind the VS Code tasks.

These scripts used to live in `.vscode/`, which is editor configuration and not a
script home. Eight Python files were in there, and two of them were load-bearing far
outside the editor: `task_artifact_runner.py` is what `scripts/lint-all.py` and
`scripts/run-tests.py` both wrap every invocation in, and `sync_claude_to_agents.py`
was the real implementation behind the shared `sync-agents` contract path — so the
workspace's lint, test and agent-context tasks all reached into `.vscode/` to work.

They are in `scripts/` now, with the naming split devkit uses: kebab-case for
entrypoints, snake_case for anything imported as a module (`ingest_fmp_tickers`, which
`aggregate-tickers.py` imports as a sibling).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts"
TASKS_JSON = REPO_ROOT / ".vscode" / "tasks.json"

# VS Code reads tasks.json as JSONC, and ours carries a comment block explaining the label
# convention. Match strings first so a `//` inside one (the DATABASE_URL values) is kept.
_JSONC_STRING_OR_COMMENT = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.DOTALL)


def strip_jsonc_comments(text: str) -> str:
    return _JSONC_STRING_OR_COMMENT.sub(
        lambda match: match.group(0) if match.group(0).startswith('"') else "", text
    )


def load_script(name: str) -> ModuleType:
    path = SCRIPT_DIR / name
    module_name = f"_test_vscode_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_strip_jsonc_comments_keeps_double_slashes_inside_strings():
    source = '{\n  // a comment\n  "url": "postgresql://trader@host:5433/db", /* trailing */\n}'

    assert strip_jsonc_comments(source) == '{\n  \n  "url": "postgresql://trader@host:5433/db", \n}'


def test_every_task_script_reference_exists():
    """A task pointing at a moved script fails only when someone clicks it.

    This is the check that made the `.vscode/` -> `scripts/` move safe: eight files
    moved and every reference to them had to follow, including two the workspace's own
    shared tasks reach through (`task-artifact-runner.py` via lint-all/run-tests, and
    the agent-context sync).
    """
    raw = strip_jsonc_comments(TASKS_JSON.read_text(encoding="utf-8"))
    tasks = json.loads(raw)["tasks"]
    prefix = "${workspaceFolder}\\scripts\\"
    references = [
        argument
        for task in tasks
        for argument in [task.get("command", ""), *task.get("args", [])]
        if argument.startswith(prefix) and argument.endswith(".py")
    ]

    assert references
    for reference in references:
        assert (SCRIPT_DIR / reference.removeprefix(prefix)).is_file(), reference


def test_no_python_lives_under_dot_vscode():
    """`.vscode/` is editor configuration, not a script home.

    Eight scripts were in there, and the two that mattered most were invisible from the
    outside: `scripts/lint-all.py` and `scripts/run-tests.py` — the paths the SHARED
    workspace tasks call — both wrapped every invocation in `.vscode/
    task_artifact_runner.py`, and `scripts/sync-agents-context.py` was a runpy shim over
    `.vscode/sync_claude_to_agents.py`. So three of this project's contract entrypoints
    silently depended on the editor directory.
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


def test_sync_claude_helpers_copy_content_and_respect_exclusions(monkeypatch, tmp_path):
    script = load_script("sync-agents-context.py")
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
