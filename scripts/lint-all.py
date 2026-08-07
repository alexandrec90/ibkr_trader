#!/usr/bin/env python3
"""Workspace-task contract for IBKR lint, formatting, and type checks.

Usage: python scripts/lint-all.py [--changed] [--no-secrets]

  --changed     lint changed Python files only (mypy still runs the full src scope)
  --no-secrets  compatibility no-op

`--changed` is the flag the vendored Stop hook names in its failure report ("fix:
python scripts/lint-all.py --changed | ..."), and scripts/hooks/tests/
test_repo_contract.py checks this usage text declares it — a runner that exited 2
on the gate's own advice is worse than no advice.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "task-artifact-runner.py"


def changed_python_files() -> list[str]:
    """Return modified and untracked Python paths that still exist."""
    commands = (
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    paths: set[str] = set()
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            paths.update(result.stdout.splitlines())
    return sorted(path for path in paths if path.endswith(".py") and (ROOT / path).is_file())


def run_artifact(name: str, module_args: list[str]) -> int:
    command = [
        "uv",
        "run",
        "python",
        str(RUNNER),
        "--artifact",
        name,
        "--",
        *module_args,
    ]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed", action="store_true", help="lint changed Python files only")
    parser.add_argument("--no-secrets", action="store_true", help="compatibility no-op")
    args = parser.parse_args(argv)

    targets = changed_python_files() if args.changed else ["src", "tests", "scripts"]
    if args.changed and not targets:
        print("lint-all: no changed Python files; nothing to do")
        return 0

    failures = 0
    failures += bool(run_artifact("lint", ["-m", "ruff", "check", *targets]))
    failures += bool(run_artifact("format-check", ["-m", "ruff", "format", "--check", *targets]))
    # A partial mypy invocation is misleading because imports pull the package graph
    # back in anyway. Run the configured source scope whenever Python changed.
    failures += bool(run_artifact("typecheck", ["-m", "mypy", "src"]))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
