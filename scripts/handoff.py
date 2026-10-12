#!/usr/bin/env python3
"""Hand the collector's database from one PC to the other through the archive bucket.

The collector (`app` plus its TimescaleDB `db`) runs on one of two Windows PCs, and the
database lives in whichever PC's `pgdata` volume. Moving it is a `pg_dump -Fc` on one side
and a restore on the other, with the dump parked in the same S3-compatible bucket the cold
archive uses (`ARCHIVE_S3_*` in `.env`, docs/operations/remote-archive.md). devkit's
`scripts/collectors.py` drives it: it is stdlib-only and cannot talk to the bucket, so it
stops and starts the containers and calls this script for everything in between.

    uv run --extra archive python scripts/handoff.py export --from PC-A --to PC-B
    uv run --extra archive python scripts/handoff.py pending --host PC-B
    uv run --extra archive python scripts/handoff.py import --host PC-B

Two objects, under `<prefix>/handoff/ibkr_trader/` (the prefix joined the way the archive
joins it, so a bare bucket has no leading segment):

- `db.dump` -- the last dump. Kept after an import: it is the last handoff's backup, and
  the next export overwrites it.
- `marker.json` -- `{"state": "exported", "from", "to", "at", "key", "sha256", "bytes",
  "alembic"}`, written only after the dump uploaded; an import rewrites it with
  `"state": "imported", "by", "imported_at"` and keeps the rest.

Exit codes: 0 success, 1 failed (the reason is in the artifact), 2 cannot tell -- only
`pending` uses it, for a bucket it cannot read. Every run overwrites `logs/handoff.json`
with `{"command", "ok", "detail", ...}`, on failure too; stdout is one status line, or for
`pending` the marker JSON when a handoff waits for this host and nothing otherwise.

The restore is **serial**. A parallel `pg_restore -j` broke TimescaleDB's catalog the last
time this was done by hand, so `-j` must never appear here; the test suite asserts it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "logs" / "handoff.json"

PROJECT = "ibkr_trader"
DUMP_NAME = "db.dump"
MARKER_NAME = "marker.json"
CONTAINER_DUMP = "/tmp/handoff.dump"

EXIT_OK, EXIT_FAILED, EXIT_CANNOT_TELL = 0, 1, 2

QUICK_TIMEOUT = 120.0
#: A dump or restore of the ~12 GB database, or a copy of its 1.8 GB dump, in seconds.
HEAVY_TIMEOUT = 4 * 3600.0
READY_TIMEOUT = 180.0
READY_POLL = 2.0

#: pg_restore entries a TimescaleDB restore emits that leave the data intact. Anything
#: else fails the import. The DB is dropped and created empty first, so an "already
#: exists" can only name what `CREATE EXTENSION` made, never a table of ours.
IGNORABLE_RESTORE_ERRORS = (
    re.compile(r"must be owner of extension", re.IGNORECASE),
    re.compile(r'extension "[^"]+" already exists', re.IGNORECASE),
    re.compile(r'schema "public" already exists', re.IGNORECASE),
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class HandoffError(Exception):
    """A step failed; the message becomes the artifact's `detail`."""


class CannotTell(HandoffError):
    """The bucket could not be read, so whether a handoff is pending is unknown."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[list[str], float], CommandResult]


def run_command(argv: list[str], timeout: float) -> CommandResult:
    """The real runner: argv list, no shell, from the checkout so compose finds its file."""
    try:
        proc = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timed out after {timeout:.0f}s")
    except OSError as exc:
        return CommandResult(127, "", f"cannot run {argv[0]}: {exc}")
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def _tail(text: str, lines: int = 5) -> str:
    return " | ".join(text.strip().splitlines()[-lines:])


@dataclass(frozen=True)
class DbIdentity:
    user: str
    database: str


class Compose:
    """`docker compose` in the checkout, through an injectable runner."""

    def __init__(self, runner: Runner):
        self.runner = runner

    def call(self, args: list[str], timeout: float = QUICK_TIMEOUT) -> CommandResult:
        return self.runner(["docker", "compose", *args], timeout)

    def run(self, args: list[str], what: str, timeout: float = QUICK_TIMEOUT) -> str:
        result = self.call(args, timeout)
        if result.returncode != 0:
            raise HandoffError(f"{what} failed (exit {result.returncode}): {_tail(result.stderr)}")
        return result.stdout

    def exec_db(self, args: list[str], what: str, timeout: float = QUICK_TIMEOUT) -> str:
        return self.run(["exec", "-T", "db", *args], what, timeout)

    def psql(self, ident: DbIdentity, database: str, sql: str) -> str:
        argv = ["psql", "-v", "ON_ERROR_STOP=1", "-U", ident.user, "-d", database, "-At"]
        return self.exec_db([*argv, "-c", sql], f"psql {sql!r}").strip()

    def remove_container_dump(self) -> None:
        """Best effort: a leftover only costs space in the container, and raising here
        would hide the error that sent us through the `finally`."""
        self.call(["exec", "-T", "db", "rm", "-f", CONTAINER_DUMP])


def db_identity(compose: Compose) -> DbIdentity:
    """The role and database the running `db` container was created with.

    Read from the container's own environment rather than re-deriving compose's
    interpolation of `.env`: it is what initdb used, whatever `.env` says today.
    """
    out = compose.exec_db(["printenv", "POSTGRES_USER", "POSTGRES_DB"], "read db identity")
    values = out.split()
    if len(values) != 2 or not all(_IDENTIFIER.fullmatch(v) for v in values):
        raise HandoffError(f"unexpected POSTGRES_USER/POSTGRES_DB in the db container: {values}")
    return DbIdentity(user=values[0], database=values[1])


@dataclass(frozen=True)
class Store:
    """The bucket: a boto3-style client plus where the handoff objects live."""

    client: Any
    bucket: str
    prefix: str = ""

    def key(self, name: str) -> str:
        return object_key(self.prefix, name)


def object_key(prefix: str, name: str) -> str:
    """`<prefix>/handoff/ibkr_trader/<name>`, joined like `S3ObjectStore._key`."""
    base = f"handoff/{PROJECT}/{name}"
    prefix = prefix.strip("/")
    return f"{prefix}/{base}" if prefix else base


def store_from_settings(settings: Any) -> Store:
    """The bucket the archive is configured with, whatever `ARCHIVE_BACKEND` says."""
    if not settings.archive_s3_bucket:
        raise CannotTell("ARCHIVE_S3_BUCKET is not set in .env")
    try:
        from data_lake.archive.store import S3ObjectStore

        s3 = S3ObjectStore.from_settings(settings)
    except (ImportError, RuntimeError) as exc:
        raise CannotTell(f"cannot build the S3 client: {exc}") from exc
    return Store(client=s3.client, bucket=s3.bucket, prefix=s3.prefix)


def _is_missing(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    return isinstance(error, dict) and str(error.get("Code")) in {"404", "NoSuchKey", "NotFound"}


def read_marker(store: Store) -> dict[str, Any] | None:
    """The marker, or None when there is none. One GET; CannotTell on anything else."""
    try:
        response = store.client.get_object(Bucket=store.bucket, Key=store.key(MARKER_NAME))
        body = response["Body"].read()
    except Exception as exc:
        if _is_missing(exc):
            return None
        raise CannotTell(f"cannot read the marker: {type(exc).__name__}: {exc}") from exc
    try:
        marker = json.loads(body)
    except ValueError as exc:
        raise CannotTell(f"the marker is not JSON: {exc}") from exc
    if not isinstance(marker, dict):
        raise CannotTell("the marker is not a JSON object")
    return marker


def write_marker(store: Store, marker: dict[str, Any]) -> None:
    try:
        store.client.put_object(
            Bucket=store.bucket,
            Key=store.key(MARKER_NAME),
            Body=json.dumps(marker, indent=2).encode(),
            ContentType="application/json",
        )
    except Exception as exc:
        raise HandoffError(f"writing the marker failed: {type(exc).__name__}: {exc}") from exc


def pending_reason(marker: dict[str, Any] | None, host: str) -> str | None:
    """None when `marker` is a handoff waiting for `host`, else why it is not."""
    if marker is None:
        return "no handoff marker"
    if marker.get("state") != "exported":
        return f"last handoff already {marker.get('state')} by {marker.get('by')}"
    if str(marker.get("to", "")).casefold() != host.casefold():
        return f"handoff addressed to {marker.get('to')}, not {host}"
    return None


def file_digest(path: Path) -> tuple[str, int]:
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return digest, path.stat().st_size


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Deps:
    """Everything with a side effect, so a test can replace each one."""

    compose: Compose
    store_factory: Callable[[], Store]
    tmp_root: Path | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def export(deps: Deps, from_host: str, to_host: str) -> dict[str, Any]:
    """Dump `db`, upload it, then -- only then -- write the marker addressed to `to_host`."""
    if from_host.casefold() == to_host.casefold():
        raise HandoffError(f"--from and --to are the same host ({from_host})")
    store = deps.store_factory()
    compose = deps.compose
    ident = db_identity(compose)
    alembic = compose.psql(ident, ident.database, "SELECT version_num FROM alembic_version")
    workdir = Path(tempfile.mkdtemp(prefix="ibkr-handoff-", dir=deps.tmp_root))
    try:
        local = workdir / DUMP_NAME
        try:
            compose.exec_db(
                ["pg_dump", "-Fc", "-U", ident.user, "-d", ident.database, "-f", CONTAINER_DUMP],
                "pg_dump",
                HEAVY_TIMEOUT,
            )
            compose.run(
                ["cp", f"db:{CONTAINER_DUMP}", str(local)], "copying the dump out", HEAVY_TIMEOUT
            )
        finally:
            compose.remove_container_dump()
        sha256, size = file_digest(local)
        key = store.key(DUMP_NAME)
        try:
            store.client.upload_file(str(local), store.bucket, key)
        except Exception as exc:
            raise HandoffError(f"upload failed: {type(exc).__name__}: {exc}") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    marker = {
        "state": "exported",
        "from": from_host,
        "to": to_host,
        "at": iso_utc(deps.now()),
        "key": key,
        "sha256": sha256,
        "bytes": size,
        "alembic": alembic,
    }
    write_marker(store, marker)
    return marker


def verify_dump(path: Path, marker: dict[str, Any]) -> None:
    sha256, size = file_digest(path)
    if size != marker.get("bytes"):
        raise HandoffError(f"dump is {size} bytes, the marker says {marker.get('bytes')}")
    if sha256 != marker.get("sha256"):
        raise HandoffError(f"dump sha256 {sha256} does not match the marker's")


def wait_ready(deps: Deps, timeout: float = READY_TIMEOUT) -> None:
    """Until Postgres accepts TCP connections, bounded.

    TCP, not the socket: on a fresh volume the image's initdb runs a temporary server that
    listens on the socket only, and would read as ready before the real one starts.
    """
    deadline = deps.clock() + timeout
    while True:
        result = deps.compose.call(["exec", "-T", "db", "pg_isready", "-h", "127.0.0.1"])
        if result.returncode == 0:
            return
        if deps.clock() >= deadline:
            raise HandoffError(f"db not ready after {timeout:.0f}s: {_tail(result.stderr)}")
        deps.sleep(READY_POLL)


def judge_restore(result: CommandResult) -> list[str]:
    """The ignored pg_restore errors when the restore counts as good; raises otherwise.

    pg_restore without `--exit-on-error` keeps going past a failed statement, prints each
    as a `pg_restore: error:` entry and exits 1. A TimescaleDB dump emits a few that are
    harmless (`IGNORABLE_RESTORE_ERRORS`), so exit 1 passes when every entry is one of
    them. Any other entry, any other exit code, or exit 1 with no entry at all fails.
    """
    if result.returncode == 0:
        return []
    entries = restore_errors(result.stderr)
    if result.returncode != 1 or not entries:
        raise HandoffError(f"pg_restore failed (exit {result.returncode}): {_tail(result.stderr)}")
    fatal = [e for e in entries if not any(p.search(e) for p in IGNORABLE_RESTORE_ERRORS)]
    if fatal:
        raise HandoffError(f"pg_restore: {len(fatal)} error(s), first: {fatal[0][:400]}")
    return entries


def restore_errors(stderr: str) -> list[str]:
    """Each `pg_restore: error:` entry with its continuation lines (`Command was: ...`)."""
    entries: list[str] = []
    current: list[str] | None = None
    for line in stderr.splitlines():
        if line.startswith("pg_restore:"):
            if current:
                entries.append(" ".join(current))
            current = [line] if line.startswith("pg_restore: error:") else None
        elif current is not None:
            current.append(line.strip())
    if current:
        entries.append(" ".join(current))
    return entries


def restore(compose: Compose, ident: DbIdentity, local: Path) -> list[str]:
    """Recreate the database and restore `local` into it, serially. Returns ignored errors."""
    db = ident.database
    compose.psql(ident, "postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
    compose.psql(ident, "postgres", f"CREATE DATABASE {db}")
    compose.psql(ident, db, "CREATE EXTENSION IF NOT EXISTS timescaledb")
    compose.psql(ident, db, "SELECT timescaledb_pre_restore()")
    try:
        compose.run(
            ["cp", str(local), f"db:{CONTAINER_DUMP}"], "copying the dump in", HEAVY_TIMEOUT
        )
        argv = ["pg_restore", "-Fc", "--no-owner", "-U", ident.user, "-d", db, CONTAINER_DUMP]
        ignored = judge_restore(compose.call(["exec", "-T", "db", *argv], HEAVY_TIMEOUT))
    except HandoffError:
        # Leave the half-restored database out of restoring mode, so its background
        # workers are not silently off; the import still fails.
        post = ["psql", "-U", ident.user, "-d", db, "-c", "SELECT timescaledb_post_restore()"]
        compose.call(["exec", "-T", "db", *post])
        raise
    finally:
        compose.remove_container_dump()
    compose.psql(ident, db, "SELECT timescaledb_post_restore()")
    compose.psql(ident, db, "ANALYZE")
    return ignored


def check_restored(compose: Compose, ident: DbIdentity, marker: dict[str, Any]) -> int:
    """The restored schema is the exported one and holds tables; returns the table count."""
    alembic = compose.psql(ident, ident.database, "SELECT version_num FROM alembic_version")
    if marker.get("alembic") and alembic != marker["alembic"]:
        raise HandoffError(f"restored alembic {alembic!r}, exported {marker['alembic']!r}")
    count = compose.psql(
        ident,
        ident.database,
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
    )
    if not count.isdigit() or int(count) == 0:
        raise HandoffError(f"restored database has no public tables ({count!r})")
    return int(count)


def import_dump(deps: Deps, host: str) -> dict[str, Any]:
    """Restore the dump addressed to `host`, then mark the handoff imported. Never starts app."""
    store = deps.store_factory()
    marker = read_marker(store)
    reason = pending_reason(marker, host)
    if marker is None or reason is not None:
        raise HandoffError(f"nothing to import: {reason}")
    compose = deps.compose
    workdir = Path(tempfile.mkdtemp(prefix="ibkr-handoff-", dir=deps.tmp_root))
    try:
        local = workdir / DUMP_NAME
        key = str(marker.get("key") or store.key(DUMP_NAME))
        try:
            store.client.download_file(store.bucket, key, str(local))
        except Exception as exc:
            raise HandoffError(f"download failed: {type(exc).__name__}: {exc}") from exc
        verify_dump(local, marker)
        compose.run(["up", "-d", "db"], "starting db")
        wait_ready(deps)
        compose.run(["--profile", "app", "stop", "app"], "stopping app")
        ident = db_identity(compose)
        ignored = restore(compose, ident, local)
        tables = check_restored(compose, ident, marker)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    updated = {**marker, "state": "imported", "by": host, "imported_at": iso_utc(deps.now())}
    write_marker(store, updated)
    return {"marker": updated, "tables": tables, "ignored_restore_errors": ignored}


def write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def run(args: argparse.Namespace, deps: Deps) -> tuple[int, dict[str, Any], str]:
    """One subcommand: (exit code, artifact payload, stdout line or "")."""
    command = args.command
    try:
        if command == "pending":
            marker = read_marker(deps.store_factory())
            reason = pending_reason(marker, args.host)
            payload = {"ok": True, "pending": reason is None, "marker": marker}
            line = json.dumps(marker) if reason is None else ""
            return (
                EXIT_OK,
                {**payload, "detail": reason or f"handoff pending for {args.host}"},
                line,
            )
        if command == "export":
            marker = export(deps, args.from_host, args.to_host)
            detail = f"exported {marker['bytes']} bytes for {args.to_host}"
            return EXIT_OK, {"ok": True, "detail": detail, "marker": marker}, f"handoff: {detail}"
        result = import_dump(deps, args.host)
        detail = f"imported {result['marker']['bytes']} bytes, {result['tables']} tables"
        return EXIT_OK, {"ok": True, "detail": detail, **result}, f"handoff: {detail}"
    except HandoffError as exc:
        unknown = command == "pending" and isinstance(exc, CannotTell)
        code = EXIT_CANNOT_TELL if unknown else EXIT_FAILED
        return code, {"ok": False, "detail": str(exc)}, f"handoff {command} FAILED: {exc}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="dump db, upload it, mark it for --to")
    exp.add_argument("--from", dest="from_host", required=True)
    exp.add_argument("--to", dest="to_host", required=True)
    pend = sub.add_parser("pending", help="print the marker if a handoff waits for --host")
    pend.add_argument("--host", required=True)
    imp = sub.add_parser("import", help="restore the handoff addressed to --host")
    imp.add_argument("--host", required=True)
    return parser.parse_args(argv)


def _settings_store() -> Store:
    from ibkr_trader.config import Settings

    # pydantic-settings ranks the process environment above this file, as everywhere else.
    return store_from_settings(Settings(_env_file=REPO_ROOT / ".env"))


def main(
    argv: list[str] | None = None,
    deps: Deps | None = None,
    artifact: Path = ARTIFACT,
) -> int:
    args = parse_args(argv)
    deps = deps or Deps(compose=Compose(run_command), store_factory=_settings_store)
    head = {"command": args.command, **{k: v for k, v in vars(args).items() if k != "command"}}
    try:
        code, payload, line = run(args, deps)
    except Exception as exc:
        # A bug, not a reported failure: leave the artifact, then let the traceback out --
        # except from `pending`, whose caller reads any exit but 0 and 1 as "cannot tell".
        code = EXIT_CANNOT_TELL if args.command == "pending" else EXIT_FAILED
        detail = f"{type(exc).__name__}: {exc}"
        write_artifact(artifact, {**head, "exit_code": code, "ok": False, "detail": detail})
        if code == EXIT_CANNOT_TELL:
            print(f"handoff pending FAILED: {detail} ({artifact})", file=sys.stderr)
            raise SystemExit(code) from exc
        raise
    write_artifact(artifact, {**head, "exit_code": code, **payload})
    if code == EXIT_OK:
        if line:
            print(line)
    elif args.command == "pending":
        print(f"{line} ({artifact})", file=sys.stderr)
    else:
        print(f"{line} ({artifact})")
    return code


if __name__ == "__main__":
    sys.exit(main())
