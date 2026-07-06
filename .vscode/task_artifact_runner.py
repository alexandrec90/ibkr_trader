"""Run a Python module command and capture noisy output in an artifact log."""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess
import sys
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True, help="Base name for the output log.")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Python arguments to run after a literal '--', for example: -- -m pytest",
    )
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("missing command after --")
    return args


def main() -> int:
    args = parse_args()
    workspace = pathlib.Path(__file__).resolve().parents[1]
    artifact_dir = workspace / "artifacts" / "tasks"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{args.artifact}.log"

    command = [sys.executable, *args.command]
    started = dt.datetime.now(dt.UTC)
    start_time = time.monotonic()

    result = subprocess.run(
        command,
        cwd=workspace,
        text=True,
        capture_output=True,
        check=False,
    )

    elapsed = time.monotonic() - start_time
    finished = dt.datetime.now(dt.UTC)

    log = [
        f"artifact: {args.artifact}",
        f"started_utc: {started.isoformat(timespec='seconds')}",
        f"finished_utc: {finished.isoformat(timespec='seconds')}",
        f"elapsed_seconds: {elapsed:.2f}",
        f"cwd: {workspace}",
        f"exit_code: {result.returncode}",
        f"command: {' '.join(command)}",
        "",
        "===== stdout =====",
        result.stdout.rstrip(),
        "",
        "===== stderr =====",
        result.stderr.rstrip(),
        "",
    ]
    artifact_path.write_text("\n".join(log), encoding="utf-8")

    status = "passed" if result.returncode == 0 else f"failed ({result.returncode})"
    relative_artifact = artifact_path.relative_to(workspace)
    print(f"{args.artifact}: {status} in {elapsed:.1f}s")
    print(f"artifact: {relative_artifact}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
