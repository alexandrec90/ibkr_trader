#!/usr/bin/env python3
"""Bring the dev database to the migrations' head, or say whether it is there.

The contract devkit's fix pass calls (`fix_migrate.py` there) every half hour, so the
database `db-revision.py` diffs against is never left behind the merged migrations --
which is what makes autogenerate propose changes that are already written. carameli's
runs Alembic inside its app container; this one runs it on the host through the uv venv,
at Postgres on 5433, exactly as `db-revision.py` does. What is shared is the CLI:

  python scripts/db-upgrade.py --check   # 0 at head, 1 behind, 2 db not up; writes nothing
  python scripts/db-upgrade.py           # apply every pending revision; 0 applied

Any other exit is a failure the pass files. Which branch the checkout is on is the
pass's question, not this script's: run by hand from a task branch, it applies that
branch's migrations. Failures go to `artifacts/tasks/db-upgrade.log`, overwritten per
run.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "artifacts" / "tasks" / "db-upgrade.log"
CURRENT, BEHIND, UNKNOWN, FAILED = 0, 1, 2, 3

# `db-revision.py`'s default, and a test holds the two equal: host port 5433, because
# another local Postgres owns 5432. The environment wins, then the checkout's `.env`.
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://trader:trader@127.0.0.1:5433/ibkr_trader?connect_timeout=5"
)
# What psycopg says when the server is up and refuses the credential.
_REFUSED = "password authentication failed"
CONNECT_TIMEOUT = 3.0
# `probe`'s answers, and the request it sends: length 8, then code 80877103.
ANSWERS, SILENT, ABSENT = "answers", "silent", "absent"
_SSL_REQUEST = (8).to_bytes(4, "big") + (80877103).to_bytes(4, "big")
ALEMBIC_TIMEOUT = 600

# `f7b8c9d0e1f2 (head)`, `f7b8c9d0e1f2 (head) (mergepoint)`: an id and its tags alone on
# the line. Alembic's log lines carry text outside parentheses and never match.
_REVISION = re.compile(r"^(\w+)(?:\s+\([^)]*\))*\s*$")


def database_source(env: dict[str, str] | None = None, root: Path | None = None) -> tuple[str, str]:
    """`(url, where it came from)`: the environment, then `root/.env`, then the default.

    `Settings` reads `.env` itself, but this script hands Alembic `DATABASE_URL`
    explicitly, and an environment variable beats `.env` there -- so a script that
    skipped the file would override the machine's real password with the default's.
    The fix pass runs it with no `.env` loaded, which is how devkit ledger a7f7c321
    filed `password authentication failed for user "trader"`."""
    source = os.environ if env is None else env
    if source.get("DATABASE_URL"):
        return source["DATABASE_URL"], "the environment"
    try:
        lines = (
            ((REPO_ROOT if root is None else root) / ".env")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    except OSError:
        lines = []
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.strip() == "DATABASE_URL" and value.strip().strip("\"'"):
            return value.strip().strip("\"'"), ".env"
    return DEFAULT_DATABASE_URL, "the built-in default"


def database_url(env: dict[str, str] | None = None, root: Path | None = None) -> str:
    """The connection string `database_source` resolves."""
    return database_source(env, root)[0]


def refused(output: list[str]) -> bool:
    """Whether Alembic's output is the server refusing the credential. Pure."""
    return any(_REFUSED in line for line in output)


def address(url: str) -> tuple[str, int]:
    """`(host, port)` the URL connects to, Postgres's default port when it names none."""
    parts = urlsplit(url)
    return parts.hostname or "127.0.0.1", parts.port or 5432


def probe(url: str, timeout: float = CONNECT_TIMEOUT) -> str:
    """`ANSWERS` when a Postgres server replies where the database should be, `SILENT`
    when something accepts the connection and never replies, `ABSENT` when nothing does.

    Asked before Alembic so a database the host cannot reach is "cannot tell" (2), which
    the pass reads as nothing to do yet, rather than a connection error it would file. A
    TCP accept alone does not show that: Docker Desktop's port forward keeps accepting on
    `127.0.0.1:5433` after an unclean engine restart while handing nothing on to the
    container, and Alembic then died on "server closed the connection unexpectedly"
    (devkit ledger fc1f3937). So the probe sends the SSLRequest a client opens with,
    which every Postgres answers with one byte (`S` or `N`) before any credential."""
    try:
        conn = socket.create_connection(address(url), timeout=timeout)
    except OSError:
        return ABSENT
    with conn:
        try:
            conn.sendall(_SSL_REQUEST)
            return ANSWERS if conn.recv(1) else SILENT
        except OSError:
            return SILENT


def reachable(url: str, timeout: float = CONNECT_TIMEOUT) -> bool:
    """Whether a Postgres server answers where the database should be (`probe`)."""
    return probe(url, timeout) == ANSWERS


def unreachable_message(state: str, url: str) -> str:
    """What `main` prints for a database the probe found no server at. Pure."""
    host, port = address(url)
    if state == SILENT:
        return (
            f"{host}:{port} accepts connections but no Postgres answers there -- a Docker"
            " port forward left dead by an engine restart reads this way;"
            " `docker compose restart db` re-creates it"
        )
    return f"nothing accepts connections on {host}:{port}; is `db` up?"


def python_exe(root: Path = REPO_ROOT) -> str:
    """The project venv's interpreter, falling back to the current one (`db-revision.py`'s
    reason: a task's PATH is not the venv's)."""
    candidate = root / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else sys.executable


def alembic_argv(*args: str, root: Path = REPO_ROOT) -> list[str]:
    return [python_exe(root), "-m", "alembic", *args]


def revisions(lines: list[str]) -> set[str]:
    """The revision ids `alembic current` or `alembic heads` printed. Pure."""
    return {match.group(1) for line in lines if (match := _REVISION.match(line.strip()))}


def verdict(current: set[str], heads: set[str]) -> int:
    """CURRENT when the database sits on every head, BEHIND otherwise. Pure."""
    return CURRENT if current == heads else BEHIND


def alembic(url: str, *args: str) -> tuple[list[str], int]:
    """Run `alembic <args>` against `url`; `(non-empty output lines, exit code)`."""
    try:
        done = subprocess.run(
            alembic_argv(*args),
            cwd=REPO_ROOT,
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=ALEMBIC_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [f"ran past {ALEMBIC_TIMEOUT}s"], FAILED
    output = [ln for ln in (done.stdout + done.stderr).splitlines() if ln.strip()]
    return output, done.returncode


def write_artifact(lines: list[str]) -> None:
    """Overwrite per run, on success too -- a stale artifact misleads the next agent."""
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    header = "# source: scripts/db-upgrade.py\n" if lines else ""
    ARTIFACT.write_text(header + "\n".join(lines), encoding="utf-8")


def fail(what: str, output: list[str], where: str = "") -> int:
    """Record a failure; the last stderr line is what the pass files.

    A refused credential stays a failure, not "cannot tell": the server is up and
    answering, waiting cures nothing, and exiting 2 would let the database fall behind
    the migrations in silence. So the filed line says which DATABASE_URL was refused."""
    if refused(output):
        what += f": the database refused the password in DATABASE_URL from {where or 'here'}"
    write_artifact([what, "", *output])
    print(f"error: {what} -- details in {ARTIFACT.relative_to(REPO_ROOT)}", file=sys.stderr)
    return FAILED


def check(url: str, where: str = "") -> int:
    current, code = alembic(url, "current")
    if code != 0:
        return fail(f"alembic current exited {code}", current, where)
    heads, code = alembic(url, "heads")
    if code != 0:
        return fail(f"alembic heads exited {code}", heads, where)
    write_artifact([])
    found = verdict(revisions(current), revisions(heads))
    print("at head" if found == CURRENT else "behind head")
    return found


def upgrade(url: str, where: str = "") -> int:
    output, code = alembic(url, "upgrade", "head")
    for line in output:
        print(f"  {line}")
    if code != 0:
        return fail(f"alembic upgrade head exited {code}", output, where)
    write_artifact([])
    print("upgraded to head")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report, apply nothing")
    args = parser.parse_args(argv)
    url, where = database_source()
    state = probe(url)
    if state != ANSWERS:
        print(unreachable_message(state, url))
        return UNKNOWN
    return check(url, where) if args.check else upgrade(url, where)


if __name__ == "__main__":
    raise SystemExit(main())
