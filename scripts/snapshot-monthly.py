#!/usr/bin/env python3
"""Run the monthly forward-shadow snapshot ritual, start to finish.

This replaces SIX VS Code tasks: "Snapshot: Run Monthly" plus the five hidden steps it
depended on ("Start Postgres", "Apply Migrations", "Ingest Price Batches", "Aggregate
Tickers", and the "Prepare Monthly Data" aggregate that sequenced them). The sequence
was encoded in `.vscode/tasks.json` as a `dependsOrder: sequence` chain, and one link
in it was a pwsh one-liner embedded in a JSON string doing `$LASTEXITCODE` arithmetic
to decide whether two ingests had both succeeded.

Three things were wrong with that, and all three are why this file exists:

  - It was a shell script wearing a task costume. The workspace rule is that scripts
    under `scripts/` are Python, so the logic can be imported and unit-tested instead
    of being retyped into a JSON escape sequence.
  - Five of the six tasks were `"hide": true` — invisible in the picker, so the only
    way to read the ritual was to reconstruct it from `dependsOn` edges.
  - `DATABASE_URL` was inlined, credentials and all, into five separate task entries.
    It is resolved once here, and the environment wins.

Every step's failure is persisted to `artifacts/tasks/snapshot-monthly.log` rather than
left in the terminal, and the run stops at the first failing step: continuing past a
failed migration would ingest into a schema that does not match the models.

Usage:
    python scripts/snapshot-monthly.py                 # the whole ritual
    python scripts/snapshot-monthly.py --skip-ingest    # re-run the snapshot only
    python scripts/snapshot-monthly.py --dry-run        # print the plan, run nothing
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = REPO_ROOT / "artifacts" / "tasks" / "snapshot-monthly.log"

# Host port 5433, not 5432: another local Postgres already owns the default (CLAUDE.md).
# The environment wins so a machine with a real `.env` is not overridden by a default
# that used to be copy-pasted into five task definitions.
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://trader:trader@127.0.0.1:5433/ibkr_trader?connect_timeout=5"
)

# These two still live under `.vscode/`, which is editor configuration and not a script
# home — they should move to `scripts/` so they are importable and testable like
# everything else. Left in place here deliberately: moving them is its own change, and
# this one is about collapsing the task graph.
INGEST_TASK = REPO_ROOT / "scripts" / "ingest-task.py"
AGGREGATE_TICKERS = REPO_ROOT / "scripts" / "aggregate-tickers.py"

READY_TIMEOUT = 60
READY_INTERVAL = 3


@dataclass(frozen=True)
class Step:
    """One named step. `argv` is built lazily so --dry-run can print without running."""

    name: str
    argv: list[str]


def database_url(env: dict[str, str] | None = None) -> str:
    """The connection string, environment first. Pure, so the precedence is testable."""
    source = os.environ if env is None else env
    return source.get("DATABASE_URL") or DEFAULT_DATABASE_URL


def python_exe(root: Path = REPO_ROOT) -> str:
    """The project venv's interpreter, falling back to the current one.

    VS Code launches tasks with its own PATH, not the venv's, so a bare `python` here
    resolves to whatever the desktop picked and fails on the first project import. The
    fallback keeps the script runnable from an already-activated shell and from CI.
    """
    candidate = root / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else sys.executable


def plan(skip_ingest: bool = False, root: Path = REPO_ROOT) -> list[Step]:
    """The ordered ritual. Pure — this is what `--dry-run` prints and what tests assert.

    Order is load-bearing: migrations before ingest (the connectors write columns the
    revision adds), prices before ticker aggregation (aggregation reads them), and the
    snapshot last, because it is the thing the other four exist to feed.
    """
    py = python_exe(root)
    steps = [
        Step("start-postgres", ["docker", "compose", "up", "-d", "db"]),
        Step(
            "wait-for-postgres",
            ["docker", "compose", "exec", "-T", "db", "pg_isready", "-U", "trader"],
        ),
        Step("apply-migrations", [py, "-m", "alembic", "upgrade", "head"]),
    ]
    if not skip_ingest:
        # Previously the pwsh one-liner: run BOTH, then fail if either did. Yahoo is
        # not skipped because FMP failed — they are independent sources and a partial
        # month is still worth having.
        steps += [
            Step("ingest-fmp-prices", [py, str(INGEST_TASK), "fmp-prices-batch"]),
            Step("ingest-yahoo-prices", [py, str(INGEST_TASK), "yahoo-prices-batch"]),
            Step("aggregate-tickers", [py, str(AGGREGATE_TICKERS)]),
        ]
    cli = root / ".venv" / "Scripts" / "ibkr-trader.exe"
    snapshot = [str(cli)] if cli.is_file() else [py, "-m", "ibkr_trader.cli"]
    steps.append(Step("snapshot-run", [*snapshot, "snapshot", "run", "--all"]))
    return steps


# Steps whose failure must not stop the run. `wait-for-postgres` is polled rather than
# trusted once, and the two ingests are independent sources — see `plan`.
TOLERATED: frozenset[str] = frozenset({"ingest-fmp-prices", "ingest-yahoo-prices"})


def wait_for_postgres(env: dict[str, str], timeout: int = READY_TIMEOUT) -> bool:
    """Poll `pg_isready` until it answers. `up -d` returns before the DB accepts."""
    deadline = time.monotonic() + timeout
    argv = ["docker", "compose", "exec", "-T", "db", "pg_isready", "-U", "trader"]
    while time.monotonic() < deadline:
        result = subprocess.run(argv, cwd=REPO_ROOT, env=env, capture_output=True, check=False)
        if result.returncode == 0:
            return True
        time.sleep(READY_INTERVAL)
    return False


def write_artifact(sections: list[str]) -> None:
    """Overwrite per run, on success too — a stale artifact misleads the next agent."""
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    header = "# source: scripts/snapshot-monthly.py\n" if sections else ""
    ARTIFACT.write_text(header + "\n".join(sections), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-ingest", action="store_true", help="re-run the snapshot only")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    # Redundant against the default, and deliberately so: a VS Code `${input:...}`
    # picker must supply one real token in EVERY branch, because an empty string does
    # not vanish from the args array — it arrives as a stray positional. This is the
    # affirming token for the "full ritual" branch of `snapshotScope`, the same reason
    # devkit's new-project.py carries `--dry-run` alongside `--yes`.
    parser.add_argument(
        "--full", action="store_true", help="run every step (the default; for the task picker)"
    )
    args, _unknown = parser.parse_known_args(argv)

    steps = plan(skip_ingest=args.skip_ingest)
    if args.dry_run:
        print("snapshot-monthly: plan (nothing will run)")
        for step in steps:
            print(f"  {step.name}: {' '.join(step.argv)}")
        return 0

    env = {**os.environ, "DATABASE_URL": database_url()}
    failures: list[str] = []

    for step in steps:
        print(f"\nsnapshot-monthly: {step.name}")
        if step.name == "wait-for-postgres":
            if wait_for_postgres(env):
                print("  ready")
                continue
            failures.append(f"# {step.name}\n# fix: docker compose logs db\ntimed out")
            break

        result = subprocess.run(
            step.argv, cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            print("  ok")
            continue

        body = (result.stdout + result.stderr).strip()
        failures.append(f"# {step.name}\n# fix: {' '.join(step.argv)}\n{body}")
        print(f"  FAILED (exit {result.returncode})")
        if step.name not in TOLERATED:
            # Continuing past a failed migration would ingest into a schema that does
            # not match the models, which corrupts the month rather than skipping it.
            print(f"  stopping: {step.name} is not recoverable mid-ritual")
            break

    write_artifact(failures)
    if failures:
        print(f"\nsnapshot-monthly: FAILED — details in {ARTIFACT.relative_to(REPO_ROOT)}")
        return 1
    print(f"\nsnapshot-monthly: complete (artifact cleared: {ARTIFACT.relative_to(REPO_ROOT)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
