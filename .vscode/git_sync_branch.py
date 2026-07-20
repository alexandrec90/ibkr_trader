"""Rebase the current clean branch onto ``origin/main`` without stashing changes.

This script intentionally refuses dirty worktrees and detached HEADs. It is used by the
``git: sync branch with origin/main (no stash)`` VS Code task, but is also safe to run from a
terminal with ``python .vscode/git_sync_branch.py``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_git(*args: str, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    """Run Git from the repository root and return its result without raising."""
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=capture_output,
        check=False,
    )


def _captured_git(*args: str) -> tuple[int, str]:
    result = run_git(*args, capture_output=True)
    if result.returncode != 0 and result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    return result.returncode, result.stdout.strip()


def main() -> int:
    status_code, changes = _captured_git("status", "--porcelain")
    if status_code != 0:
        return status_code
    if changes:
        print(
            "Working tree is not clean. Commit your changes before syncing; "
            "this task will never stash them.",
            file=sys.stderr,
        )
        return 1

    branch_code, branch = _captured_git("branch", "--show-current")
    if branch_code != 0:
        return branch_code
    if not branch:
        print(
            "Cannot sync while HEAD is detached. Check out a branch first.",
            file=sys.stderr,
        )
        return 1

    print(f"Syncing {branch!r} with origin/main...")
    fetched = run_git("fetch", "--prune", "origin")
    if fetched.returncode != 0:
        return fetched.returncode

    ref_code, _ = _captured_git("rev-parse", "--verify", "--quiet", "refs/remotes/origin/main")
    if ref_code != 0:
        print("origin/main was not found after fetching.", file=sys.stderr)
        return 1

    return run_git("rebase", "origin/main").returncode


if __name__ == "__main__":
    raise SystemExit(main())
