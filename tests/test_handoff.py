"""Tests for `scripts/handoff.py`, the collector database handoff through the bucket.

No Docker and no network: the compose runner and the S3 client are fakes. The fake runner
answers each `docker compose` argv the way the real container would and records the
sequence, so the restore order -- and the absence of `-j` -- is asserted command by command.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "handoff.py"


def load_handoff() -> ModuleType:
    """Import `scripts/handoff.py`, registered first so `@dataclass` can find its module."""
    spec = importlib.util.spec_from_file_location("handoff_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


handoff = load_handoff()

DUMP = b"PGDMP fake custom-format dump" * 100
DUMP_SHA = hashlib.sha256(DUMP).hexdigest()
ALEMBIC = "f00dfeed1234"
NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)
KEY = "handoff/ibkr_trader/db.dump"
MARKER_KEY = "handoff/ibkr_trader/marker.json"


class MissingKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    """The boto3 client surface the script uses, over a dict."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})
        self.fail: dict[str, Exception] = {}
        self.calls: list[str] = []

    def _maybe_fail(self, op: str) -> None:
        self.calls.append(op)
        if op in self.fail:
            raise self.fail[op]

    def get_object(self, Bucket, Key):
        self._maybe_fail("get_object")
        if Key not in self.objects:
            raise MissingKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self._maybe_fail("put_object")
        self.objects[Key] = Body

    def upload_file(self, Filename, Bucket, Key):
        self._maybe_fail("upload_file")
        self.objects[Key] = Path(Filename).read_bytes()

    def download_file(self, Bucket, Key, Filename):
        self._maybe_fail("download_file")
        Path(Filename).write_bytes(self.objects[Key])


class FakeCompose:
    """Answers `docker compose ...` argvs like a healthy stack, recording each one."""

    def __init__(self, restore=None, ready=None, alembic=ALEMBIC, tables="42"):
        self.calls: list[list[str]] = []
        self.restore = restore or handoff.CommandResult(0)
        self.ready = list(ready or [0])
        self.alembic = alembic
        self.tables = tables
        self.copied_in: bytes | None = None

    def __call__(self, argv: list[str], timeout: float):
        assert argv[:2] == ["docker", "compose"]
        args = argv[2:]
        self.calls.append(args)
        result = handoff.CommandResult
        if "printenv" in args:
            return result(0, "trader\nibkr_trader\n")
        if "pg_isready" in args:
            return result(self.ready.pop(0) if len(self.ready) > 1 else self.ready[0], "", "no")
        if "pg_restore" in args:
            return self.restore
        if "psql" in args:
            sql = args[-1]
            if "alembic_version" in sql:
                return result(0, self.alembic + "\n")
            if "information_schema.tables" in sql:
                return result(0, self.tables + "\n")
            return result(0)
        if args[0] == "cp":
            src, dst = args[1], args[2]
            if src.startswith("db:"):
                Path(dst).write_bytes(DUMP)
            else:
                self.copied_in = Path(src).read_bytes()
        return result(0)

    def sql(self) -> list[tuple[str, str]]:
        """(database, statement) for every psql call, in order."""
        return [(c[c.index("-d") + 1], c[-1]) for c in self.calls if "psql" in c]


def exported_marker(**overrides):
    marker = {
        "state": "exported",
        "from": "PC-A",
        "to": "PC-B",
        "at": "2026-10-11T11:00:00Z",
        "key": KEY,
        "sha256": DUMP_SHA,
        "bytes": len(DUMP),
        "alembic": ALEMBIC,
    }
    return {**marker, **overrides}


def make_deps(tmp_path, runner, s3):
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir(parents=True)
    return handoff.Deps(
        compose=handoff.Compose(runner),
        store_factory=lambda: handoff.Store(client=s3, bucket="bucket"),
        tmp_root=tmp_root,
        now=lambda: NOW,
        clock=iter(range(10_000)).__next__,
        sleep=lambda _s: None,
    )


def stored_marker(s3: FakeS3) -> dict:
    return json.loads(s3.objects[MARKER_KEY])


# --- keys, markers, small helpers -------------------------------------------


def test_object_key_joins_the_prefix_like_the_archive():
    assert handoff.object_key("", "db.dump") == "handoff/ibkr_trader/db.dump"
    assert handoff.object_key("/lake/", "marker.json") == "lake/handoff/ibkr_trader/marker.json"
    assert handoff.Store(client=None, bucket="b", prefix="p").key("db.dump") == (
        "p/handoff/ibkr_trader/db.dump"
    )


@pytest.mark.parametrize(
    ("marker", "pending"),
    [
        (None, False),
        (exported_marker(), True),
        (exported_marker(to="pc-b"), True),
        (exported_marker(to="PC-C"), False),
        (exported_marker(state="imported", by="PC-B"), False),
    ],
)
def test_pending_reason(marker, pending):
    assert (handoff.pending_reason(marker, "PC-B") is None) is pending


def test_read_marker_none_when_missing_and_cannot_tell_otherwise():
    s3 = FakeS3()
    store = handoff.Store(client=s3, bucket="b")
    assert handoff.read_marker(store) is None
    s3.objects[MARKER_KEY] = b"not json"
    with pytest.raises(handoff.CannotTell):
        handoff.read_marker(store)
    s3.objects[MARKER_KEY] = b"[1]"
    with pytest.raises(handoff.CannotTell):
        handoff.read_marker(store)
    s3.fail["get_object"] = ConnectionError("offline")
    with pytest.raises(handoff.CannotTell, match="offline"):
        handoff.read_marker(store)


def test_write_marker_failure_is_a_handoff_error():
    s3 = FakeS3()
    s3.fail["put_object"] = ConnectionError("down")
    with pytest.raises(handoff.HandoffError, match="writing the marker"):
        handoff.write_marker(handoff.Store(client=s3, bucket="b"), {"state": "x"})


def test_store_from_settings_needs_a_bucket():
    settings = SimpleNamespace(archive_s3_bucket="")
    with pytest.raises(handoff.CannotTell, match="ARCHIVE_S3_BUCKET"):
        handoff.store_from_settings(settings)


def test_store_from_settings_builds_the_archive_client():
    pytest.importorskip("boto3")
    settings = SimpleNamespace(
        archive_backend="s3",
        archive_s3_bucket="bucket",
        archive_s3_endpoint_url="https://example.invalid",
        archive_s3_region="auto",
        archive_s3_access_key_id="fake-id",
        archive_s3_secret_access_key="fake-secret",
        archive_s3_prefix="/pre/",
    )
    store = handoff.store_from_settings(settings)
    assert (store.bucket, store.prefix) == ("bucket", "pre")
    assert store.key("marker.json") == "pre/handoff/ibkr_trader/marker.json"


def test_file_digest_and_iso_utc(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(DUMP)
    assert handoff.file_digest(path) == (DUMP_SHA, len(DUMP))
    assert handoff.iso_utc(NOW) == "2026-10-11T12:00:00Z"


def test_verify_dump(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(DUMP)
    handoff.verify_dump(path, exported_marker())
    with pytest.raises(handoff.HandoffError, match="bytes"):
        handoff.verify_dump(path, exported_marker(bytes=1))
    with pytest.raises(handoff.HandoffError, match="sha256"):
        handoff.verify_dump(path, exported_marker(sha256="0" * 64))


def test_db_identity_reads_the_container_env_and_refuses_odd_values():
    compose = handoff.Compose(FakeCompose())
    assert handoff.db_identity(compose) == handoff.DbIdentity("trader", "ibkr_trader")

    def odd(argv, timeout):
        return handoff.CommandResult(0, "trader\nibkr; drop\n")

    with pytest.raises(handoff.HandoffError, match="unexpected"):
        handoff.db_identity(handoff.Compose(odd))


def test_compose_run_raises_with_the_stderr_tail():
    def failing(argv, timeout):
        return handoff.CommandResult(3, "", "line1\nboom")

    with pytest.raises(handoff.HandoffError, match="exit 3.*boom"):
        handoff.Compose(failing).run(["up"], "starting db")


def test_run_command_runs_argv_without_a_shell():
    ok = handoff.run_command([sys.executable, "-c", "print('hi')"], 30)
    assert (ok.returncode, ok.stdout.strip()) == (0, "hi")
    missing = handoff.run_command(["definitely-not-a-binary-xyz"], 5)
    assert missing.returncode == 127
    slow = handoff.run_command([sys.executable, "-c", "import time; time.sleep(5)"], 0.2)
    assert slow.returncode == 124


def test_wait_ready_polls_until_ready_then_gives_up(tmp_path):
    runner = FakeCompose(ready=[2, 2, 0])
    handoff.wait_ready(make_deps(tmp_path, runner, FakeS3()))
    assert sum("pg_isready" in c for c in runner.calls) == 3
    assert ["exec", "-T", "db", "pg_isready", "-h", "127.0.0.1"] in runner.calls

    never = FakeCompose(ready=[2])
    with pytest.raises(handoff.HandoffError, match="not ready"):
        handoff.wait_ready(make_deps(tmp_path / "b", never, FakeS3()), timeout=5)


# --- pg_restore success criteria ---------------------------------------------

OWNER_ERROR = (
    "pg_restore: error: could not execute query: ERROR:  must be owner of extension timescaledb\n"
    "Command was: COMMENT ON EXTENSION timescaledb IS 'Enables scalable inserts';\n"
    "pg_restore: warning: errors ignored on restore: 1\n"
)
REAL_ERROR = (
    'pg_restore: error: could not execute query: ERROR:  relation "price_bars" does not exist\n'
    "Command was: COPY public.price_bars FROM stdin;\n"
    "pg_restore: warning: errors ignored on restore: 1\n"
)


def test_restore_errors_groups_continuation_lines():
    entries = handoff.restore_errors(OWNER_ERROR + REAL_ERROR)
    assert len(entries) == 2
    assert "COMMENT ON EXTENSION" in entries[0]
    assert "COPY public.price_bars" in entries[1]


def test_judge_restore_clean_exit_passes():
    assert handoff.judge_restore(handoff.CommandResult(0)) == []


def test_judge_restore_ignorable_errors_pass():
    ignored = handoff.judge_restore(handoff.CommandResult(1, "", OWNER_ERROR))
    assert len(ignored) == 1


@pytest.mark.parametrize(
    "result",
    [
        handoff.CommandResult(1, "", REAL_ERROR),
        handoff.CommandResult(1, "", OWNER_ERROR + REAL_ERROR),
        handoff.CommandResult(1, "", "pg_restore: warning: something odd\n"),
        handoff.CommandResult(2, "", OWNER_ERROR),
    ],
)
def test_judge_restore_anything_else_fails(result):
    with pytest.raises(handoff.HandoffError, match="pg_restore"):
        handoff.judge_restore(result)


# --- export -------------------------------------------------------------------


def test_export_uploads_then_writes_the_marker(tmp_path):
    runner, s3 = FakeCompose(), FakeS3()
    deps = make_deps(tmp_path, runner, s3)
    marker = handoff.export(deps, "PC-A", "PC-B")

    assert s3.objects[KEY] == DUMP
    assert s3.calls == ["upload_file", "put_object"]
    assert stored_marker(s3) == marker == exported_marker(at="2026-10-11T12:00:00Z")
    dump_cmd = next(c for c in runner.calls if "pg_dump" in c)
    assert (
        dump_cmd == "exec -T db pg_dump -Fc -U trader -d ibkr_trader -f /tmp/handoff.dump".split()
    )
    assert ["exec", "-T", "db", "rm", "-f", "/tmp/handoff.dump"] in runner.calls
    assert list(deps.tmp_root.iterdir()) == []


def test_export_upload_failure_writes_no_marker_and_cleans_up(tmp_path):
    runner, s3 = FakeCompose(), FakeS3()
    s3.fail["upload_file"] = ConnectionError("network")
    deps = make_deps(tmp_path, runner, s3)
    with pytest.raises(handoff.HandoffError, match="upload failed"):
        handoff.export(deps, "PC-A", "PC-B")
    assert MARKER_KEY not in s3.objects
    assert "put_object" not in s3.calls
    assert list(deps.tmp_root.iterdir()) == []


def test_export_pg_dump_failure_still_removes_the_container_copy(tmp_path):
    def runner(argv, timeout):
        if "pg_dump" in argv:
            return handoff.CommandResult(1, "", "disk full")
        return base(argv, timeout)

    base = FakeCompose()
    s3 = FakeS3()
    deps = make_deps(tmp_path, runner, s3)
    with pytest.raises(handoff.HandoffError, match="pg_dump failed.*disk full"):
        handoff.export(deps, "PC-A", "PC-B")
    assert base.calls[-1] == ["exec", "-T", "db", "rm", "-f", "/tmp/handoff.dump"]
    assert s3.objects == {}
    assert list(deps.tmp_root.iterdir()) == []


def test_export_refuses_the_same_host_twice(tmp_path):
    runner = FakeCompose()
    with pytest.raises(handoff.HandoffError, match="same host"):
        handoff.export(make_deps(tmp_path, runner, FakeS3()), "pc-a", "PC-A")
    assert runner.calls == []


# --- import -------------------------------------------------------------------


def test_import_restores_serially_in_order_and_marks_imported(tmp_path):
    runner = FakeCompose()
    s3 = FakeS3({KEY: DUMP, MARKER_KEY: json.dumps(exported_marker()).encode()})
    deps = make_deps(tmp_path, runner, s3)
    result = handoff.import_dump(deps, "pc-b")

    restore_cmd = next(c for c in runner.calls if "pg_restore" in c)
    assert restore_cmd == (
        "exec -T db pg_restore -Fc --no-owner -U trader -d ibkr_trader /tmp/handoff.dump".split()
    )
    assert not any(a == "-j" or a.startswith(("-j", "--jobs")) for c in runner.calls for a in c)
    assert runner.copied_in == DUMP

    # The whole sequence, with psql calls reduced to (database, statement).
    def step(c):
        return ("psql", c[c.index("-d") + 1], c[-1]) if "psql" in c else tuple(c)

    restore_at = runner.calls.index(restore_cmd)
    assert [step(c) for c in runner.calls] == [
        ("up", "-d", "db"),
        ("exec", "-T", "db", "pg_isready", "-h", "127.0.0.1"),
        ("--profile", "app", "stop", "app"),
        ("exec", "-T", "db", "printenv", "POSTGRES_USER", "POSTGRES_DB"),
        ("psql", "postgres", "DROP DATABASE IF EXISTS ibkr_trader WITH (FORCE)"),
        ("psql", "postgres", "CREATE DATABASE ibkr_trader"),
        ("psql", "ibkr_trader", "CREATE EXTENSION IF NOT EXISTS timescaledb"),
        ("psql", "ibkr_trader", "SELECT timescaledb_pre_restore()"),
        ("cp", runner.calls[restore_at - 1][1], "db:/tmp/handoff.dump"),
        tuple(restore_cmd),
        ("exec", "-T", "db", "rm", "-f", "/tmp/handoff.dump"),
        ("psql", "ibkr_trader", "SELECT timescaledb_post_restore()"),
        ("psql", "ibkr_trader", "ANALYZE"),
        ("psql", "ibkr_trader", "SELECT version_num FROM alembic_version"),
        (
            "psql",
            "ibkr_trader",
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
        ),
    ]
    assert not any("up" in c and "app" in c for c in runner.calls)  # devkit starts app

    marker = stored_marker(s3)
    assert marker == {
        **exported_marker(),
        "state": "imported",
        "by": "pc-b",
        "imported_at": "2026-10-11T12:00:00Z",
    }
    assert result["tables"] == 42
    assert s3.objects[KEY] == DUMP  # kept as the last handoff's backup
    assert list(deps.tmp_root.iterdir()) == []


@pytest.mark.parametrize(
    ("marker", "match"),
    [
        (None, "no handoff marker"),
        (exported_marker(to="PC-C"), "addressed to PC-C"),
        (exported_marker(state="imported", by="PC-B"), "already imported"),
    ],
)
def test_import_with_nothing_pending_touches_nothing(tmp_path, marker, match):
    runner = FakeCompose()
    s3 = FakeS3({KEY: DUMP})
    if marker is not None:
        s3.objects[MARKER_KEY] = json.dumps(marker).encode()
    with pytest.raises(handoff.HandoffError, match=f"nothing to import: .*{match}"):
        handoff.import_dump(make_deps(tmp_path, runner, s3), "PC-B")
    assert runner.calls == []
    assert "put_object" not in s3.calls


def test_import_sha_mismatch_fails_before_docker(tmp_path):
    runner = FakeCompose()
    s3 = FakeS3({KEY: DUMP + b"x", MARKER_KEY: json.dumps(exported_marker()).encode()})
    deps = make_deps(tmp_path, runner, s3)
    with pytest.raises(handoff.HandoffError, match="bytes"):
        handoff.import_dump(deps, "PC-B")
    assert runner.calls == []
    assert stored_marker(s3)["state"] == "exported"
    assert list(deps.tmp_root.iterdir()) == []


def test_import_failed_restore_keeps_the_marker_pending(tmp_path):
    runner = FakeCompose(restore=handoff.CommandResult(1, "", REAL_ERROR))
    s3 = FakeS3({KEY: DUMP, MARKER_KEY: json.dumps(exported_marker()).encode()})
    deps = make_deps(tmp_path, runner, s3)
    with pytest.raises(handoff.HandoffError, match="does not exist"):
        handoff.import_dump(deps, "PC-B")
    assert stored_marker(s3)["state"] == "exported"
    # Restoring mode is switched off again, and the container copy removed.
    assert runner.calls[-2][-1] == "SELECT timescaledb_post_restore()"
    assert runner.calls[-1] == ["exec", "-T", "db", "rm", "-f", "/tmp/handoff.dump"]
    assert list(deps.tmp_root.iterdir()) == []


def test_import_ignorable_restore_errors_pass(tmp_path):
    runner = FakeCompose(restore=handoff.CommandResult(1, "", OWNER_ERROR))
    s3 = FakeS3({KEY: DUMP, MARKER_KEY: json.dumps(exported_marker()).encode()})
    result = handoff.import_dump(make_deps(tmp_path, runner, s3), "PC-B")
    assert len(result["ignored_restore_errors"]) == 1
    assert stored_marker(s3)["state"] == "imported"


@pytest.mark.parametrize(
    ("alembic", "tables", "match"),
    [("0ther", "42", "alembic"), (ALEMBIC, "0", "no public tables")],
)
def test_check_restored_sanity(alembic, tables, match):
    compose = handoff.Compose(FakeCompose(alembic=alembic, tables=tables))
    ident = handoff.DbIdentity("trader", "ibkr_trader")
    with pytest.raises(handoff.HandoffError, match=match):
        handoff.check_restored(compose, ident, exported_marker())


def test_restore_returns_ignored_errors_directly(tmp_path):
    runner = FakeCompose(restore=handoff.CommandResult(1, "", OWNER_ERROR))
    dump = tmp_path / "d"
    dump.write_bytes(DUMP)
    ident = handoff.DbIdentity("trader", "ibkr_trader")
    assert len(handoff.restore(handoff.Compose(runner), ident, dump)) == 1


# --- the CLI: exit codes, stdout, artifact ------------------------------------


def test_parse_args_requires_the_hosts():
    args = handoff.parse_args(["export", "--from", "A", "--to", "B"])
    assert (args.command, args.from_host, args.to_host) == ("export", "A", "B")
    with pytest.raises(SystemExit):
        handoff.parse_args(["import"])


def run_main(tmp_path, argv, runner, s3):
    artifact = tmp_path / "logs" / "handoff.json"
    code = handoff.main(argv, make_deps(tmp_path, runner, s3), artifact)
    return code, json.loads(artifact.read_text(encoding="utf-8"))


def test_main_pending_prints_the_marker_for_this_host(tmp_path, capsys):
    s3 = FakeS3({MARKER_KEY: json.dumps(exported_marker()).encode()})
    runner = FakeCompose()
    code, artifact = run_main(tmp_path, ["pending", "--host", "PC-B"], runner, s3)
    assert code == 0
    assert json.loads(capsys.readouterr().out) == exported_marker()
    assert artifact["pending"] is True and artifact["ok"] is True
    assert runner.calls == [] and s3.calls == ["get_object"]  # one GET, no Docker


def test_main_pending_elsewhere_prints_nothing(tmp_path, capsys):
    s3 = FakeS3({MARKER_KEY: json.dumps(exported_marker(to="PC-C")).encode()})
    code, artifact = run_main(tmp_path, ["pending", "--host", "PC-B"], FakeCompose(), s3)
    assert (code, capsys.readouterr().out) == (0, "")
    assert artifact["pending"] is False


def test_main_pending_unreachable_store_exits_2(tmp_path, capsys):
    s3 = FakeS3()
    s3.fail["get_object"] = ConnectionError("no route")
    code, artifact = run_main(tmp_path, ["pending", "--host", "PC-B"], FakeCompose(), s3)
    assert code == 2
    assert capsys.readouterr().out == ""
    assert artifact["ok"] is False and "no route" in artifact["detail"]


def test_main_pending_unconfigured_store_exits_2(tmp_path):
    def unconfigured():
        return handoff.store_from_settings(SimpleNamespace(archive_s3_bucket=""))

    deps = make_deps(tmp_path, FakeCompose(), FakeS3())
    deps.store_factory = unconfigured
    artifact = tmp_path / "a.json"
    assert handoff.main(["pending", "--host", "PC-B"], deps, artifact) == 2


def test_main_export_failure_exits_1_and_writes_the_artifact(tmp_path, capsys):
    s3 = FakeS3()
    s3.fail["upload_file"] = ConnectionError("network")
    code, artifact = run_main(tmp_path, ["export", "--from", "A", "--to", "B"], FakeCompose(), s3)
    assert code == 1
    assert artifact["command"] == "export" and artifact["ok"] is False
    assert "upload failed" in artifact["detail"]
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "FAILED" in out


def test_main_export_and_import_succeed(tmp_path, capsys):
    s3 = FakeS3()
    code, artifact = run_main(tmp_path, ["export", "--from", "A", "--to", "B"], FakeCompose(), s3)
    assert code == 0 and artifact["marker"]["to"] == "B"
    code, artifact = run_main(tmp_path / "x", ["import", "--host", "B"], FakeCompose(), s3)
    assert code == 0 and artifact["marker"]["state"] == "imported"
    assert capsys.readouterr().out.count("\n") == 2


def test_main_import_nothing_pending_exits_1(tmp_path):
    code, artifact = run_main(tmp_path, ["import", "--host", "B"], FakeCompose(), FakeS3())
    assert code == 1 and "nothing to import" in artifact["detail"]


def test_main_unexpected_exception_leaves_the_artifact(tmp_path, capsys):
    def boom():
        raise OSError("disk")

    deps = make_deps(tmp_path, FakeCompose(), FakeS3())
    deps.store_factory = boom
    artifact = tmp_path / "a.json"

    with pytest.raises(OSError, match="disk"):  # a bug keeps its traceback: exit 1
        handoff.main(["import", "--host", "B"], deps, artifact)
    written = json.loads(artifact.read_text(encoding="utf-8"))
    assert (written["exit_code"], written["ok"], written["detail"]) == (1, False, "OSError: disk")

    with pytest.raises(SystemExit) as exit_info:  # but `pending` stays "cannot tell"
        handoff.main(["pending", "--host", "B"], deps, artifact)
    assert exit_info.value.code == 2
    assert json.loads(artifact.read_text(encoding="utf-8"))["exit_code"] == 2
    assert capsys.readouterr().out == ""


def test_run_reports_a_handoff_error_as_exit_1():
    deps = handoff.Deps(compose=handoff.Compose(FakeCompose()), store_factory=lambda: None)
    code, payload, line = handoff.run(
        handoff.parse_args(["export", "--from", "A", "--to", "a"]), deps
    )
    assert code == 1 and "same host" in payload["detail"] and "FAILED" in line


def test_write_artifact_creates_the_directory(tmp_path):
    path = tmp_path / "logs" / "handoff.json"
    handoff.write_artifact(path, {"ok": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
