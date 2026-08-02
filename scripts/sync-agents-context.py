#!/usr/bin/env python3
"""Expose the existing IBKR context sync at the shared workspace contract path."""

from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    namespace = runpy.run_path(str(ROOT / ".vscode" / "sync_claude_to_agents.py"))
    namespace["main"]()


if __name__ == "__main__":
    main()
