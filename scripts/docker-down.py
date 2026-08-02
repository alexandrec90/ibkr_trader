#!/usr/bin/env python3
"""Stop this project's containers across every profile it defines.

Satisfies the `docker-down` half of the shared task contract: devkit's
`scripts/docker-maint.py` looks for `scripts/docker-down.py` in the checkout and
delegates to it, which is what lets the workspace's one "Docker: Stop Stack" task work
here. It exists as a script rather than as a `docker compose down` line in a task
because of the profiles — `ib-gateway` is under `ibkr` and `app` is under `app`
(docker-compose.yml), and a bare `down` leaves both running while reporting success.

That is precisely the bug this replaced. "Stop: Docker Stack" existed in both this
project and carameli with the same label and different commands behind it, so the
answer to "what does Stop do" depended on which window you were in.

Named volumes are never touched: `pgdata` holds the ingested bar history, and
re-ingesting it is measured in hours against rate-limited APIs. `-v`/`--volumes` must
never appear here — this runs from a one-click task over a project picker.

Usage:
    python scripts/docker-down.py             # every profile
    python scripts/docker-down.py --profile db-only
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every profile in docker-compose.yml. Services with no `profiles:` key (db) always
# participate, so they need no entry — listing them would be a no-op that reads like
# a requirement.
PROFILES = ("ibkr", "app")


def compose_argv(profiles: tuple[str, ...] = PROFILES) -> list[str]:
    """The teardown command. Pure, so the flag order is testable without Docker.

    Split out mostly to make the volume guarantee assertable: a test can prove `-v`
    is absent without needing a Docker daemon or a real stack to tear down.
    """
    argv = ["docker", "compose"]
    for profile in profiles:
        argv += ["--profile", profile]
    return [*argv, "down"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-only",
        action="store_true",
        help="stop only the always-on services, leaving the gateway and app profiles alone",
    )
    # Accepted and ignored: devkit's docker-maint.py forwards whatever the task passed,
    # and the shared action currently sends nothing. Parsing an unexpected flag rather
    # than crashing keeps a future task argument from turning into a usage error here.
    args, _unknown = parser.parse_known_args(argv)

    command = compose_argv(() if args.db_only else PROFILES)
    print(f"docker-down: {' '.join(command)}")
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
