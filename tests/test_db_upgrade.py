"""`scripts/db-upgrade.py`: the CLI devkit's fix pass migrates this database by.

Alembic and the socket are stubbed: what is asserted is the exit code the pass reads
for each state of the database, and that nothing is applied on `--check`.
"""

from __future__ import annotations

import importlib.util
import socket
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name: str) -> ModuleType:
    path = SCRIPT_DIR / name
    module_name = f"_test_db_{path.stem.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


up = load_script("db-upgrade.py")


@pytest.fixture
def db(monkeypatch, tmp_path):
    """The database reachable, Alembic answering from `answers`, every call kept."""
    world = {
        "up": True,
        "answers": {
            ("current",): (["INFO  [alembic.runtime.migration] Context impl PostgresqlImpl."], 0),
            ("heads",): (["f7b8c9d0e1f2 (head)"], 0),
            ("upgrade", "head"): (["INFO  [alembic.runtime.migration] Running upgrade"], 0),
        },
        "ran": [],
    }

    def alembic(url, *args):
        world["ran"].append(args)
        return world["answers"][args]

    monkeypatch.setattr(up, "reachable", lambda url: world["up"])
    monkeypatch.setattr(up, "alembic", alembic)
    monkeypatch.setattr(up, "ARTIFACT", tmp_path / "artifacts" / "tasks" / "db-upgrade.log")
    monkeypatch.setattr(up, "REPO_ROOT", tmp_path)
    return world


def test_the_default_database_is_the_one_db_revision_diffs_against():
    """Two copies of one URL: an upgrade of a different database than autogenerate reads
    would leave the drift this script exists to remove."""
    assert up.DEFAULT_DATABASE_URL == load_script("db-revision.py").DEFAULT_DATABASE_URL
    assert up.database_url({"DATABASE_URL": "postgresql://real/url"}) == "postgresql://real/url"
    assert up.database_url({}) == up.DEFAULT_DATABASE_URL


def test_address_reads_the_host_and_port_out_of_a_driver_url():
    assert up.address(up.DEFAULT_DATABASE_URL) == ("127.0.0.1", 5433)
    assert up.address("postgresql://u:p@db/x") == ("db", 5432)


def test_reachable_is_false_where_nothing_listens():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert not up.reachable(f"postgresql://u:p@127.0.0.1:{port}/x", timeout=0.5)


def test_reachable_is_true_where_something_listens():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert up.reachable(f"postgresql://u:p@127.0.0.1:{port}/x", timeout=0.5)


def test_revisions_reads_ids_and_ignores_alembics_log_lines():
    lines = [
        "INFO  [alembic.runtime.migration] Will assume transactional DDL.",
        "f7b8c9d0e1f2 (head)",
        "a1b2c3d4e5f6 (head) (mergepoint)",
    ]
    assert up.revisions(lines) == {"f7b8c9d0e1f2", "a1b2c3d4e5f6"}


def test_verdict_is_current_only_on_every_head():
    assert up.verdict({"a"}, {"a"}) == up.CURRENT
    assert up.verdict(set(), {"a"}) == up.BEHIND, "an empty database is behind"
    assert up.verdict({"a"}, {"a", "b"}) == up.BEHIND


def test_alembic_runs_through_the_venv_as_a_module(tmp_path):
    assert up.alembic_argv("current", root=tmp_path)[1:] == ["-m", "alembic", "current"]


def test_python_exe_prefers_the_venv_and_falls_back_to_the_running_interpreter(tmp_path):
    assert up.python_exe(tmp_path) == sys.executable
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    assert up.python_exe(tmp_path) == str(venv_python)


def test_alembic_hands_it_the_url_and_keeps_only_non_blank_output(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs["env"]
        return SimpleNamespace(stdout="f7b8c9d0e1f2 (head)\n\n", stderr="  \nwarn\n", returncode=4)

    monkeypatch.setattr(up.subprocess, "run", run)
    assert up.alembic("postgresql://u:p@h/x", "heads") == (["f7b8c9d0e1f2 (head)", "warn"], 4)
    assert seen["argv"][-2:] == ["alembic", "heads"]
    assert seen["env"]["DATABASE_URL"] == "postgresql://u:p@h/x"


def test_an_alembic_that_runs_past_its_timeout_is_a_failure(monkeypatch):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(up.subprocess, "run", run)
    assert up.alembic("postgresql://u:p@h/x", "current") == (
        [f"ran past {up.ALEMBIC_TIMEOUT}s"],
        up.FAILED,
    )


def test_write_artifact_heads_a_failure_and_empties_on_success(db):
    up.write_artifact(["alembic heads exited 1", "", "boom"])
    assert up.ARTIFACT.read_text(encoding="utf-8") == (
        "# source: scripts/db-upgrade.py\nalembic heads exited 1\n\nboom"
    )
    up.write_artifact([])
    assert up.ARTIFACT.read_text(encoding="utf-8") == ""


def test_fail_records_the_cause_and_names_the_artifact(db, capsys):
    assert up.fail("alembic heads exited 1", ["boom"]) == up.FAILED
    assert "boom" in up.ARTIFACT.read_text(encoding="utf-8")
    err = capsys.readouterr().err
    assert err.startswith("error: alembic heads exited 1")
    assert str(Path("artifacts", "tasks", "db-upgrade.log")) in err


def test_check_reads_heads_after_current_and_applies_nothing(db):
    assert up.check("postgresql://u:p@h/x") == up.BEHIND
    db["answers"][("current",)] = (["f7b8c9d0e1f2 (head)"], 0)
    assert up.check("postgresql://u:p@h/x") == up.CURRENT
    assert db["ran"] == [("current",), ("heads",)] * 2


def test_check_fails_when_alembic_heads_cannot_answer(db):
    db["answers"][("heads",)] = (["no script_location"], 1)
    assert up.check("postgresql://u:p@h/x") == up.FAILED
    assert "alembic heads exited 1" in up.ARTIFACT.read_text(encoding="utf-8")


def test_upgrade_applies_head(db, capsys):
    assert up.upgrade("postgresql://u:p@h/x") == 0
    assert db["ran"] == [("upgrade", "head")]
    assert capsys.readouterr().out.strip().endswith("upgraded to head")


def test_check_reports_behind_and_applies_nothing(db):
    assert up.main(["--check"]) == up.BEHIND
    assert db["ran"] == [("current",), ("heads",)]


def test_check_reports_current(db):
    db["answers"][("current",)] = (["f7b8c9d0e1f2 (head)"], 0)
    assert up.main(["--check"]) == up.CURRENT


def test_a_database_that_is_not_up_is_unknown_not_a_failure(db):
    """The pass reads 2 as "nothing to do until `db` starts"."""
    db["up"] = False
    assert up.main(["--check"]) == up.UNKNOWN
    assert up.main([]) == up.UNKNOWN
    assert db["ran"] == []


def test_a_check_alembic_could_not_answer_is_a_failure_with_its_artifact(db, capsys):
    db["answers"][("current",)] = (["Can't locate revision identified by 'zzz'"], 1)
    assert up.main(["--check"]) == up.FAILED
    assert "Can't locate revision" in up.ARTIFACT.read_text(encoding="utf-8")
    assert capsys.readouterr().err.startswith("error: alembic current exited 1")


def test_upgrade_applies_head_and_empties_the_artifact(db):
    up.ARTIFACT.parent.mkdir(parents=True)
    up.ARTIFACT.write_text("stale", encoding="utf-8")
    assert up.main([]) == 0
    assert db["ran"] == [("upgrade", "head")]
    assert up.ARTIFACT.read_text(encoding="utf-8") == ""


def test_a_failed_upgrade_says_so_last_on_stderr(db, capsys):
    """The pass files the last line on stderr as the cause."""
    db["answers"][("upgrade", "head")] = (["DuplicateTable: relation exists"], 1)
    assert up.main([]) == up.FAILED
    assert (
        capsys.readouterr()
        .err.strip()
        .splitlines()[-1]
        .startswith("error: alembic upgrade head exited 1")
    )
