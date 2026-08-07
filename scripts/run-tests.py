#!/usr/bin/env python3
"""Workspace-task contract for the IBKR pytest suite.

Usage: python scripts/run-tests.py [--changed] [extra pytest args...]

  --changed   run pytest's last-failed subset (falls back to the whole suite when
              nothing failed last time)

`--changed` is not optional decoration: the vendored Stop hook signs its failure
report off with "Re-run locally: ... | python scripts/run-tests.py --changed", and
scripts/hooks/tests/test_repo_contract.py checks this usage text declares the flag.
A runner that rejected it would fail on the very advice the gate handed the agent.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "task-artifact-runner.py"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed", action="store_true", help="run pytest's last-failed subset")
    args, extra = parser.parse_known_args(argv)

    pytest_args = ["-m", "pytest"]
    if args.changed:
        pytest_args += ["--last-failed", "--last-failed-no-failures", "all"]
    pytest_args += [arg for arg in extra if arg]
    command = [
        "uv",
        "run",
        "python",
        str(RUNNER),
        "--artifact",
        "test",
        "--",
        *pytest_args,
    ]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
