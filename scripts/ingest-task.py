#!/usr/bin/env python3
"""Dispatcher behind the single "Ingest: Run Source" VS Code task.

Every ingest source used to be its own tasks.json entry -- thirteen near-identical
blocks differing only in an artifact name and a handful of CLI flags. VS Code
can't vary an args array from a picker (an input substitutes one argv element),
so the table lives here instead and the task passes a mode name.

Each mode maps to (artifact_name, argv) where argv is what
task-artifact-runner.py runs after its `--`. Modes with a `{arg}` placeholder
take the task's free-text input; the rest ignore it. DATABASE_URL comes from the
task's env and is inherited.

Usage:  python scripts/ingest-task.py <mode> [--arg VALUE]
        python scripts/ingest-task.py --list
"""

# ruff: noqa: T201, S603 - a CLI dispatcher: printing IS its output, and every argv it
# spawns is assembled from the MODES table above, never from user input.

import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent
RUNNER = SCRIPTS / "task-artifact-runner.py"
TICKER_INGEST = "scripts/ingest_fmp_tickers.py"
CLI = ["-m", "ibkr_trader.cli"]

# mode -> (artifact, argv, default_arg). `{arg}` in argv is filled from --arg.
MODES: dict[str, tuple[str, list[str], str]] = {
    "fmp-price-one": (
        "ingest-fmp-price",
        [*CLI, "ingest", "prices", "{arg}", "--source", "fmp"],
        "NVDA",
    ),
    "fmp-prices-batch": (
        "ingest-fmp-prices",
        [TICKER_INGEST, "--tickers", "tickers-fmp.txt"],
        "",
    ),
    "yahoo-prices-batch": (
        "ingest-yahoo-prices",
        [TICKER_INGEST, "--source", "yahoo", "--tickers", "tickers-yahoo.txt"],
        "",
    ),
    "yahoo-fundamentals-batch": (
        "ingest-yahoo-fundamentals",
        [TICKER_INGEST, "--source", "yahoo-fundamentals", "--tickers", "tickers-yahoo.txt"],
        "",
    ),
    "alpha-vantage-batch": (
        "ingest-alpha-vantage-batch",
        [
            *CLI,
            "ingest",
            "prices",
            "--source",
            "alpha_vantage",
            "--universe-file",
            "tickers-av.txt",
        ],
        "",
    ),
    "finnhub-news-one": (
        "ingest-finnhub-news",
        [*CLI, "ingest", "finnhub-news", "{arg}"],
        "AAPL",
    ),
    "finnhub-news-batch": (
        "ingest-finnhub-news-batch",
        [
            *CLI,
            "ingest",
            "finnhub-news",
            "--universe-file",
            "tickers.txt",
            "--spacing-seconds",
            "1.1",
        ],
        "",
    ),
    "newsapi-batch": (
        "ingest-newsapi-batch",
        [*CLI, "ingest", "news", "--mapping-file", "news-keywords.txt"],
        "",
    ),
    "reddit": (
        "ingest-reddit",
        [*CLI, "ingest", "reddit", "--limit", "{arg}"],
        "100",
    ),
    "trends-one": (
        "ingest-google-trends",
        [*CLI, "ingest", "trends", "--keywords", "{arg}"],
        "Nvidia",
    ),
    "trends-batch": (
        "ingest-google-trends-batch",
        [*CLI, "ingest", "trends", "--mapping-file", "trends-keywords.txt"],
        "",
    ),
    "fmp-fx": (
        "ingest-fmp-fx",
        [*CLI, "ingest", "fx", "--pair", "{arg}"],
        "USDCAD",
    ),
    "yahoo-fx-backfill": (
        "ingest-yahoo-fx",
        [*CLI, "ingest", "fx", "--pair", "{arg}", "--source", "yahoo"],
        "USDCAD",
    ),
}


def build_argv(mode: str, arg: str) -> list[str]:
    """Full command line for `mode`, with `{arg}` resolved. Pure — unit-testable."""
    artifact, argv, default = MODES[mode]
    value = arg.strip() or default
    resolved = [value if part == "{arg}" else part for part in argv]
    return [sys.executable, str(RUNNER), "--artifact", artifact, "--", *resolved]


def parse_args(args: list[str]) -> tuple[str | None, str]:
    """(mode, arg) from argv. mode is None when it's missing or unknown."""
    arg = ""
    positional: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--arg" and i + 1 < len(args):
            arg = args[i + 1]
            i += 2
            continue
        if args[i].startswith("--arg="):
            arg = args[i].split("=", 1)[1]
        elif not args[i].startswith("-"):
            positional.append(args[i])
        i += 1
    mode = positional[0] if positional and positional[0] in MODES else None
    return mode, arg


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if "--list" in args:
        for name, (artifact, _, default) in MODES.items():
            suffix = f"  (arg default: {default})" if default else ""
            print(f"  {name:<26} -> {artifact}{suffix}")
        return 0

    mode, arg = parse_args(args)
    if mode is None:
        print(
            f"usage: ingest_task.py <mode> [--arg VALUE]\nmodes: {', '.join(MODES)}",
            file=sys.stderr,
        )
        return 2

    cmd = build_argv(mode, arg)
    print(f"[{mode}] {' '.join(cmd[3:])}\n")
    return subprocess.run(cmd, cwd=REPO_ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
