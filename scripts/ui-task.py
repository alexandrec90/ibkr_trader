#!/usr/bin/env python3
"""Open the local UI: build the frontend if it is stale, then serve it until Ctrl+C.

The one-click entrypoint for `ibkr-trader ui`, shaped like `report-task.py` so a
devkit workspace task can dispatch to it. What it adds over the bare command is what a
terminal user does by hand once and a task must do every time:

  - **`npm ci`** when `frontend/node_modules` is missing (a fresh worktree has none).
  - **`npm run build`** when `frontend/dist/index.html` is missing or older than any
    frontend source, so the page served is the code on disk, never a stale bundle.
  - **The `[ui]` extra**, via `uv run --extra ui`, which adds fastapi/uvicorn and leaves
    every other installed extra alone. Without `uv` on PATH it falls back to the venv's
    interpreter, and the CLI's own error names the missing extra.

The server is a foreground process on 127.0.0.1 that ends with its terminal: nothing
stays resident. Under devkit's "IBKR: Open Trader UI" task, **close the terminal** to
stop it. Ctrl+C does not reach it there, because the dispatcher's `log-wrap.py` starts
this script without a console. Closing the terminal does not reach it either; it kills
only `log-wrap.py`. So this script hands its own parent's PID to the server as
`--exit-with-pid`, and the server stops when that process is gone
(`src/ibkr_trader/web/lifetime.py`). Run by hand, that parent is your shell.

It reads Postgres, so the `db` container must be up for anything but the empty states.

    python scripts/ui-task.py [--port N] [--no-open]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"

# What a build reads besides `src/`: a change to any of these makes `dist/` stale.
BUILD_INPUTS = (
    "index.html",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "vite.config.ts",
)


def python_exe(root: Path = REPO_ROOT) -> str:
    """The project venv's interpreter, falling back to the current one."""
    candidate = root / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else sys.executable


def server_prefix(uv: str | None, root: Path = REPO_ROOT) -> list[str]:
    """The prefix that runs Python with the `[ui]` extra installed. Pure."""
    return [uv, "run", "--extra", "ui", "python"] if uv else [python_exe(root)]


def build_inputs(frontend: Path) -> Iterable[Path]:
    yield from (p for p in (frontend / "src").rglob("*") if p.is_file())
    yield from (frontend / name for name in BUILD_INPUTS if (frontend / name).is_file())


def needs_build(frontend: Path) -> bool:
    """True when `dist/index.html` is missing or older than any build input."""
    built = frontend / "dist" / "index.html"
    if not built.is_file():
        return True
    stamp = built.stat().st_mtime
    return any(path.stat().st_mtime > stamp for path in build_inputs(frontend))


def npm_steps(frontend: Path, npm: str) -> list[list[str]]:
    """The npm commands to run before serving, in order; empty when `dist/` is current."""
    steps = []
    if not (frontend / "node_modules").is_dir():
        steps.append([npm, "ci", "--no-fund", "--no-audit"])
    if steps or needs_build(frontend):
        steps.append([npm, "run", "build"])
    return steps


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build (if stale) and serve the local UI.")
    parser.add_argument("--port", type=int, help="loopback port (default: UI_PORT, else 8001)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    return parser.parse_args(argv)


def serve_argv(prefix: list[str], args: argparse.Namespace, launcher_pid: int) -> list[str]:
    """The `ibkr-trader ui` command line, tied to `launcher_pid`. Pure -- unit-tested."""
    argv = [*prefix, "-m", "ibkr_trader.cli", "ui", "--exit-with-pid", str(launcher_pid)]
    if args.port:
        argv += ["--port", str(args.port)]
    if args.no_open:
        argv.append("--no-open")
    return argv


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    # Read first: once a slow npm build has run, the launcher may already be gone.
    launcher = os.getppid()
    npm = shutil.which("npm")
    steps = npm_steps(FRONTEND, npm or "npm")
    if steps and npm is None:
        print("[ui] npm is not on PATH; install Node 24+ to build frontend/", flush=True)
        return 1
    for step in steps:
        print(f"[ui] {' '.join(step[1:])} in frontend/", flush=True)
        code = subprocess.run(step, cwd=FRONTEND, check=False).returncode
        if code:
            return code
    try:
        return subprocess.run(
            serve_argv(server_prefix(shutil.which("uv")), args, launcher),
            cwd=REPO_ROOT,
            check=False,
        ).returncode
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
