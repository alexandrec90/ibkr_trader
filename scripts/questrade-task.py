#!/usr/bin/env python3
"""Recommend Questrade orders for the shared "Questrade: ..." workspace tasks.

The contract entrypoint behind devkit's `questrade-plan` action; `check` has no task row,
since keeping the token alive is a schedule's job, not a click's. Both views already exist as CLI commands (`ibkr-trader questrade plan|check`); what this
script adds is what a one-click task needs and a terminal user supplies by hand:

  - **The venv.** VS Code launches tasks with its own PATH, so the CLI runs through
    `uv run` (or the venv's interpreter when uv is not on PATH), never a bare `python`.
  - **The repo root as working directory.** The Questrade token is single-use, and the
    CLI rewrites it in `./.env` after every login -- run from anywhere else, it would find
    no token to read and no line to rewrite.
  - **A place to write.** The order tickets land in `reports/questrade-plan.csv`
    (git-ignored), never the repo root.

Views (`python scripts/questrade-task.py <view>`):

  plan --strategy NAME   laddered limit orders for every account (needs the `db` container)
  check                  each account's equity and spendable cash (Questrade only)

Advisory only: nothing here can place an order. Output streams to the terminal; devkit's
dispatcher wraps the run in `log-wrap.py`, which keeps a failed run's output under `logs/`.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_CSV = REPO_ROOT / "reports" / "questrade-plan.csv"


def python_exe(root: Path = REPO_ROOT) -> str:
    """The project venv's interpreter, falling back to the current one."""
    candidate = root / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else sys.executable


def runner(uv: str | None, root: Path = REPO_ROOT) -> list[str]:
    """The prefix that runs a command in the project venv. Pure."""
    return [uv, "run", "python"] if uv else [python_exe(root)]


def parse_args(argv: list[str]) -> argparse.Namespace:
    """`--strategy` is required, as `report-task.py lab`'s picks are: the workspace task
    always supplies it from a picker, so a default could only mask a task that had
    stopped passing one."""
    parser = argparse.ArgumentParser(description="Recommend Questrade orders (advisory only).")
    views = parser.add_subparsers(dest="view", required=True)
    plan = views.add_parser("plan", help="laddered limit orders for every account")
    plan.add_argument("--strategy", required=True, help="registered strategy name")
    views.add_parser("check", help="each account's equity and spendable cash")
    return parser.parse_args(argv)


def cli_command(args: argparse.Namespace, csv_path: Path = PLAN_CSV) -> list[str]:
    """The `ibkr-trader` argument list for the view. Pure -- unit-tested."""
    if args.view == "plan":
        return ["questrade", "plan", "--strategy", args.strategy, "--csv", str(csv_path)]
    if args.view == "check":
        return ["questrade", "check"]
    raise ValueError(f"{args.view!r} is not a view")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    command = cli_command(args)
    print(f"[questrade] ibkr-trader {' '.join(command)}", flush=True)
    argv_full = [*runner(shutil.which("uv")), "-m", "ibkr_trader.cli", *command]
    return subprocess.run(argv_full, cwd=REPO_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
