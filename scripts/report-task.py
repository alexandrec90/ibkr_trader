#!/usr/bin/env python3
"""Open the model and strategy views for the shared "Report: ..." workspace tasks.

The contract entrypoint behind devkit's `report` and `strategy-lab` actions. Every view
it opens already exists as a CLI command; what this script adds is the two things a
one-click task needs and a terminal user supplies by hand:

  - **The optional extra.** Each chart needs a dependency the core package deliberately
    goes without (plotly for `[report]`, alphalens + matplotlib for `[research]`, mlflow
    for `[tracking]`). `uv run --extra <x>` adds exactly that one to the venv and leaves
    every other installed extra alone -- `uv run` syncs inexactly, where `uv sync --extra`
    would uninstall `[ml]` to add `[report]`. Without `uv` on PATH it falls back to the
    venv's interpreter, and the CLI's own "run `uv sync --extra ...`" error says what is
    missing.
  - **A place to write.** Every HTML lands under `reports/` (git-ignored), never the
    repo root, and the view opens in the default browser.

Views (`python scripts/report-task.py <view>`):

  results   leaderboard + equity/drawdown curves of every persisted backtest_runs row
  factor    Alphalens tear sheet (IC decay, quantiles, turnover) of the latest OOS run
  models    MLflow's experiment browser over `mlruns/`, on 127.0.0.1, until Ctrl+C
  summary   the three terminal views: run leaderboard, latest model, forward shadow
  lab       re-simulate the strategy line-up over recent windows (minutes) and chart it

Everything but `models` reads Postgres, so the `db` container must be up. Output streams
to the terminal; devkit's dispatcher wraps the run in `log-wrap.py`, which keeps a failed
run's output under `logs/`.

`models` is a foreground process bound to loopback that ends with its terminal: the
local `file:` store stays the only tracking backend (docs/plans/completed/
tools-08-mlflow-tracking.md rules out a persistent tracking server, not a viewer).
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "reports"
MLRUNS_DIR = REPO_ROOT / "mlruns"

# The optional-dependency extra each view needs; `None` means the core install suffices.
EXTRAS: dict[str, str | None] = {
    "results": "report",
    "lab": "report",
    "factor": "research",
    "models": "tracking",
    "summary": None,
}

# `tracking.py` sets the same opt-in before writing: MLflow 3 refuses its FileStore
# without it, and the viewer reads that store.
MLFLOW_ENV = {"MLFLOW_ALLOW_FILE_STORE": "true"}
MLFLOW_PORT = 5000


def python_exe(root: Path = REPO_ROOT) -> str:
    """The project venv's interpreter, falling back to the current one.

    VS Code launches tasks with its own PATH, not the venv's, so a bare `python` resolves
    to whatever the desktop picked and fails on the first project import.
    """
    candidate = root / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else sys.executable


def runner(extra: str | None, uv: str | None, root: Path = REPO_ROOT) -> list[str]:
    """The prefix that runs a command in the venv with `extra` installed. Pure."""
    if uv is None:
        return [python_exe(root)]
    prefix = [uv, "run"]
    if extra:
        prefix += ["--extra", extra]
    return [*prefix, "python"]


def parse_args(argv: list[str]) -> argparse.Namespace:
    """One subcommand per view. `lab`'s options are required, as in `backtest-task.py`:
    the workspace task always supplies them from pickers, so a default could only mask
    a task that had stopped passing one."""
    parser = argparse.ArgumentParser(description="Open a model or strategy view.")
    views = parser.add_subparsers(dest="view", required=True)
    views.add_parser("results", help="backtest leaderboard + equity/drawdown charts")
    views.add_parser("factor", help="Alphalens tear sheet of the latest OOS run")
    views.add_parser("models", help="MLflow experiment browser (foreground)")
    views.add_parser("summary", help="terminal leaderboard, latest model, forward shadow")
    lab = views.add_parser("lab", help="re-simulate the strategy line-up and chart it")
    lab.add_argument("--universe", required=True, help="'sp500' or a universe file")
    lab.add_argument("--account", required=True, help="registered account")
    return parser.parse_args(argv)


def cli_commands(args: argparse.Namespace, reports: Path = REPORTS_DIR) -> list[list[str]]:
    """The `ibkr-trader` argument lists the view runs, in order. Pure -- unit-tested."""
    if args.view == "results":
        return [["report", "--output", str(reports / "backtest-results.html")]]
    if args.view == "lab":
        lab = ["backtest", "lab", "--universe", args.universe, "--account", args.account]
        return [[*lab, "--output", str(reports / "lab-report.html")]]
    if args.view == "factor":
        return [["backtest", "factor-report", "--output-dir", str(reports / "factor")]]
    if args.view == "summary":
        return [
            ["backtest", "compare", "--limit", "20"],
            ["train", "report"],
            ["snapshot", "report"],
        ]
    raise ValueError(f"{args.view!r} is not a CLI view")


def newest_report(directory: Path, pattern: str = "factor-report-run-*.html") -> Path | None:
    """The most recently written report in `directory`, or None. Pure over the filesystem."""
    candidates = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime)
    return candidates[-1] if candidates else None


def free_port(start: int = MLFLOW_PORT, attempts: int = 50) -> int:
    """The first loopback port from `start` nothing is listening on."""
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"no free port in {start}-{start + attempts - 1}")


def mlflow_argv(prefix: list[str], port: int, store: Path = MLRUNS_DIR) -> list[str]:
    """The MLflow viewer command. Pure -- unit-tested.

    `prefix` ends in an interpreter, so the viewer is reached as `-m mlflow` rather than
    an `mlflow` shim that may not be on VS Code's PATH.
    """
    return [
        *prefix,
        "-m",
        "mlflow",
        "ui",
        "--backend-store-uri",
        store.resolve().as_uri(),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]


def run_cli(prefix: list[str], commands: list[list[str]]) -> int:
    """Run each CLI command in turn; the worst exit code wins, and a failure does not stop
    the rest (a summary with no trained model still shows the backtest leaderboard)."""
    worst = 0
    for command in commands:
        argv = [*prefix, "-m", "ibkr_trader.cli", *command]
        print(f"\n[report] ibkr-trader {' '.join(command)}", flush=True)
        worst = max(worst, subprocess.run(argv, cwd=REPO_ROOT, check=False).returncode)
    return worst


def serve_mlflow(prefix: list[str]) -> int:
    """Run the viewer in the foreground and open the browser once it answers."""
    if not MLRUNS_DIR.is_dir():
        print(
            "no MLflow store at mlruns/ -- train with `ibkr-trader train run --track-mlflow` "
            "first; the authoritative metadata stays under models/ml_lt/ either way",
            flush=True,
        )
        return 1
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        mlflow_argv(prefix, port), cwd=REPO_ROOT, env={**os.environ, **MLFLOW_ENV}
    )
    print(f"[report] MLflow at {url} -- Ctrl+C or close this terminal to stop", flush=True)
    deadline = time.monotonic() + 90
    while process.poll() is None and time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                webbrowser.open(url)
                break
        time.sleep(0.5)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    prefix = runner(EXTRAS[args.view], shutil.which("uv"))
    if args.view == "models":
        return serve_mlflow(prefix)
    REPORTS_DIR.mkdir(exist_ok=True)
    code = run_cli(prefix, cli_commands(args))
    if args.view == "factor" and code == 0:
        report = newest_report(REPORTS_DIR / "factor")
        if report is not None:
            webbrowser.open(report.resolve().as_uri())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
