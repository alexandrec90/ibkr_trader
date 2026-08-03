#!/usr/bin/env python3
"""Run one backtest for the shared "Backtest: ..." workspace tasks.

Both tasks used to invoke `.venv\\Scripts\\ibkr-trader.exe` straight out of
`.vscode/tasks.json`, and that is precisely why they could not be hoisted as they stood:
the shared task block dispatches through devkit's `devkit_project.py`, whose contract is
`scripts/<name>.py` accepting documented arguments -- not a per-project executable path
that only resolves on a Windows desktop with a built venv. This script is that seam, and
it is the last thing that kept a `.vscode/tasks.json` alive in this repo.

Two things move out of a JSON string and into code by being here:

  - **The OOS windows.** `--start 2008-06-02` and `--sim-start 2010-01-04` were literals
    in a task's args array. They are fixed deliberately -- an out-of-sample run whose
    warm-up moves is not comparable to the previous one -- and a named constant with that
    sentence beside it is a better home for the promise than a JSON array nobody diffs.
    They are deliberately NOT exposed as arguments: a picker for them would be a picker
    for "make this run incomparable to the last one".
  - **The interpreter.** VS Code launches tasks with its own PATH, not the venv's, so a
    bare `python` resolves to whatever the desktop picked and dies on the first project
    import. `python_exe` does the same lookup `snapshot-monthly.py` does, and falls back
    to the running interpreter so the script still works from an activated shell and CI.

Failures land in `artifacts/tasks/backtest-run.log` or `backtest-oos.log` through
`task-artifact-runner.py` -- read the artifact, not the terminal.

Usage:
    python scripts/backtest-task.py run --strategy ml_lt_ridge --account tfsa \
        --universe-file tickers-etfs.txt --start 2008-06-02 \
        --eval-start 2010-01-04 --end 2030-01-01
    python scripts/backtest-task.py oos --account tfsa --end 2026-07-01
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "task-artifact-runner.py"

# Fixed on purpose; see the module docstring. Moving either of these invalidates every
# comparison against a previously recorded OOS run.
OOS_START = "2008-06-02"
OOS_SIM_START = "2010-01-04"

# One artifact per mode. Sharing one would mean an OOS run silently overwrote the
# evidence from the `run` that prompted it.
ARTIFACTS = {"run": "backtest-run", "oos": "backtest-oos"}


def python_exe(root: Path = REPO_ROOT) -> str:
    """The project venv's interpreter, falling back to the current one."""
    candidate = root / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else sys.executable


def parse_args(argv: list[str]) -> argparse.Namespace:
    """The two modes and their options. Every option is required.

    Required rather than defaulted because the workspace task always supplies all of
    them from pickers, so a default here could only ever mask a task that had stopped
    passing one -- silently simulating a different window than the one selected.
    """
    parser = argparse.ArgumentParser(description="Run one backtest and record it.")
    modes = parser.add_subparsers(dest="mode", required=True)

    run = modes.add_parser("run", help="simulate one allocator over one universe")
    run.add_argument("--strategy", required=True, help="allocator to simulate")
    run.add_argument("--account", required=True, help="registered account")
    run.add_argument("--universe-file", required=True, help="universe file at the repo root")
    run.add_argument("--start", required=True, help="bar-load start (YYYY-MM-DD)")
    run.add_argument("--eval-start", required=True, help="first decision date (YYYY-MM-DD)")
    run.add_argument("--end", required=True, help="window end (YYYY-MM-DD)")

    oos = modes.add_parser("oos", help="walk-forward out-of-sample evaluation")
    oos.add_argument("--account", required=True, help="registered account")
    oos.add_argument("--end", required=True, help="dataset window end (YYYY-MM-DD)")

    return parser.parse_args(argv)


def cli_args(args: argparse.Namespace) -> list[str]:
    """The `backtest <mode> ...` arguments for the project CLI. Pure -- unit-tested."""
    if args.mode == "run":
        return [
            "run",
            "--strategy",
            args.strategy,
            "--account",
            args.account,
            "--universe-file",
            args.universe_file,
            "--start",
            args.start,
            "--eval-start",
            args.eval_start,
            "--end",
            args.end,
        ]
    return [
        "oos",
        "--account",
        args.account,
        "--end",
        args.end,
        "--start",
        OOS_START,
        "--sim-start",
        OOS_SIM_START,
    ]


def build_argv(args: argparse.Namespace, root: Path = REPO_ROOT) -> list[str]:
    """Full command line, artifact wrapper included. Pure -- unit-tested.

    `-m ibkr_trader.cli` rather than the `ibkr-trader.exe` shim the old tasks named:
    `task-artifact-runner.py` prepends its own `sys.executable`, so the thing it runs has
    to be a module command. The venv is reached through the interpreter instead.
    """
    return [
        python_exe(root),
        str(root / "scripts" / "task-artifact-runner.py"),
        "--artifact",
        ARTIFACTS[args.mode],
        "--",
        "-m",
        "ibkr_trader.cli",
        "backtest",
        *cli_args(args),
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    command = build_argv(args)
    # Echo from the module command on, so the line is the backtest and not the wrapper.
    print(f"[backtest {args.mode}] {' '.join(command[5:])}\n", flush=True)
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
