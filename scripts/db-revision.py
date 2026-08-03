#!/usr/bin/env python3
"""Autogenerate an Alembic revision from the current model diff.

The contract entrypoint behind the shared "DB: New Migration (Autogenerate)" workspace
task (`devkit_project.ACTIONS["db-revision"]`). The implementation is deliberately
per-project — carameli's runs alembic inside its app container, because that stack has
PgBouncer in front of Postgres and alembic has to bypass it. What is shared is the CLI:
`-m "<message>"`.

**This one runs on the host**, through the uv-managed venv, straight at Postgres on
5433. There is no pooler here to bypass, and the `app` profile is the `ibkr-trader serve`
scheduler rather than a dev shell — routing a migration through it would mean starting a
scheduler to write a file.

`db` DOES have to be up: autogenerate is a DIFF against a live database, not a read of
the models alone. It compares `db/models.py` (which re-exports both the trading tables
and the data-lake package's, so one `Base.metadata` covers the whole schema) against
whatever the URL points at. Point it at an empty or stale database and it will
confidently propose dropping every table it cannot see — which is exactly the failure
`models.py` re-exporting both halves exists to prevent, and it is worth knowing that a
stopped container reproduces it.

**Read the generated file before committing it.** Autogenerate misses renames (it sees a
drop plus an add) and does not always get constraint or type changes right. This script
prints the path it wrote and never runs `upgrade`, so the database is untouched.

Usage: python scripts/db-revision.py -m "add sentiment score index"
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "artifacts" / "tasks" / "db-revision.log"

# Host port 5433, not 5432: another local Postgres already owns the default. The
# environment wins so a machine with a real `.env` is not overridden by this default.
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://trader:trader@127.0.0.1:5433/ibkr_trader?connect_timeout=5"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-m",
        "--message",
        required=True,
        help="short description of the schema change; becomes part of the filename",
    )
    return parser.parse_args(argv)


def database_url(env: dict[str, str] | None = None) -> str:
    """The connection string, environment first. Pure, so the precedence is testable."""
    source = os.environ if env is None else env
    return source.get("DATABASE_URL") or DEFAULT_DATABASE_URL


def python_exe(root: Path = REPO_ROOT) -> str:
    """The project venv's interpreter, falling back to the current one.

    VS Code launches tasks with its own PATH, not the venv's, so a bare `python` resolves
    to whatever the desktop picked and fails on the first project import.
    """
    candidate = root / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else sys.executable


def build_argv(message: str, root: Path = REPO_ROOT) -> list[str]:
    """Full command line. Pure — unit-tested.

    `-m alembic` through the venv interpreter rather than a bare `alembic`, for the PATH
    reason in `python_exe`. The message is passed as its own argv element, so it never
    reaches a shell and needs no quoting.
    """
    return [python_exe(root), "-m", "alembic", "revision", "--autogenerate", "-m", message]


def created_paths(output: list[str]) -> list[str]:
    """The revision files alembic reported writing, repo-relative. Pure — unit-tested."""
    paths = []
    for line in output:
        match = re.search(r"Generating\s+(\S+\.py)", line)
        if match:
            raw = match.group(1).replace("\\", "/")
            _, _, tail = raw.partition("/migrations/versions/")
            paths.append(f"migrations/versions/{tail}" if tail else raw)
    return paths


def write_artifact(lines: list[str]) -> None:
    """Overwrite per run, on success too — a stale artifact misleads the next agent."""
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    header = "# source: scripts/db-revision.py\n" if lines else ""
    ARTIFACT.write_text(header + "\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env = {**os.environ, "DATABASE_URL": database_url()}

    print("\n=== IBKR: New Migration (Autogenerate) ===")
    print(f"Artifact : {ARTIFACT.relative_to(REPO_ROOT)}")
    print(f"Message  : {args.message}\n")

    result = subprocess.run(
        build_argv(args.message),
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    output = [ln for ln in (result.stdout + result.stderr).splitlines() if ln.strip()]
    for line in output:
        print(f"  {line}")

    if result.returncode != 0:
        write_artifact(
            [
                "Failed command: python scripts/db-revision.py",
                "",
                "If this is a connection error, `db` is not up:",
                "  docker compose up -d db",
                "",
                "=== alembic revision --autogenerate ===",
                *output,
            ]
        )
        print(f"\n[FAIL] alembic exited with code {result.returncode}")
        print(f"Details: {ARTIFACT.relative_to(REPO_ROOT)}")
        return result.returncode

    write_artifact([])
    print("\n[OK] revision written:")
    for path in created_paths(output):
        print(f"  {path}")
    print("\nREAD IT before committing: autogenerate misses renames and gets some")
    print("constraint/type changes wrong. It has NOT been applied to the database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
