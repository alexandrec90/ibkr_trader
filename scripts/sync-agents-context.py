#!/usr/bin/env python3
"""Mirror the CLAUDE-side instruction files onto their AGENTS-side counterparts.

This is the `sync-agents` half of the shared task contract — devkit's
`devkit_project.py` invokes `scripts/sync-agents-context.py` in the chosen checkout,
which is why the path is fixed even though the implementation is this project's own.

It used to be two files: this path held a fourteen-line `runpy.run_path` shim whose only
job was to reach into `.vscode/sync_claude_to_agents.py` for the actual logic. The
indirection existed because the implementation predated the contract and lived in the
editor directory; with both in `scripts/` there is nothing left for a shim to bridge.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".claude",
    ".agents",
    "__pycache__",
    "node_modules",
}


def is_excluded(path: Path) -> bool:
    relative_parts = path.relative_to(ROOT).parts
    return any(part in EXCLUDED_DIRS for part in relative_parts)


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def sync_claude_markdown() -> int:
    synced_count = 0

    for path in ROOT.rglob("*"):
        if not path.is_file() or path.name not in {"CLAUDE.md", "claude.md"}:
            continue

        if is_excluded(path):
            continue

        destination = path.with_name("AGENTS.md")
        copy_file(path, destination)
        print(f"Synced {path.relative_to(ROOT)} -> {destination.relative_to(ROOT)}")
        synced_count += 1

    return synced_count


def sync_claude_config() -> int:
    claude_root = ROOT / ".claude"
    agents_root = ROOT / ".agents"
    synced_count = 0

    if not claude_root.exists():
        return synced_count

    for path in claude_root.rglob("*"):
        if not path.is_file():
            continue

        relative_path = path.relative_to(claude_root)
        destination = agents_root / relative_path
        copy_file(path, destination)
        print(f"Synced .claude/{relative_path.as_posix()} -> .agents/{relative_path.as_posix()}")
        synced_count += 1

    return synced_count


def main() -> None:
    markdown_count = sync_claude_markdown()
    config_count = sync_claude_config()
    print(f"Done. Synced {markdown_count} markdown file(s) and {config_count} .claude file(s).")


if __name__ == "__main__":
    main()
