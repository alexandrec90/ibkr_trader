"""Move checked (`- [x]`) checklist items in TODO.md to the end of the file.

Keeps the active `- [ ]` items where they are so the working sections stay uncluttered, and
consolidates every completed item under a single trailing `## ✅ Done` section (the existing
Done heading, wherever it lives, is folded into that one). Document order of the completed items
is preserved, so the current top-of-file Done list comes first, followed by items checked off in
the numbered sections.

Behavior notes:
- A "checklist item" is a `- [ ]`/`- [x]` line plus any indented continuation lines that wrap it.
- Non-list content (headings, blank lines, `**bold**` prose like the forward-shadow ritual and
  the paper-trading prerequisites) is left exactly in place.
- Idempotent: running it again on an already-tidied file is a no-op.
"""

from __future__ import annotations

import pathlib
import re
import sys

DONE_HEADING = "## ✅ Done"
_ITEM_START = re.compile(r"^- \[( |x|X)\] ")


def parse_blocks(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Group raw lines into ('checked'|'unchecked'|'other', lines) blocks."""
    blocks: list[tuple[str, list[str]]] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if _ITEM_START.match(line):
            checked = line[3].lower() == "x"
            block = [line]
            i += 1
            # Absorb wrapped continuation lines (indented, non-list, non-heading).
            while i < n:
                nxt = lines[i]
                if nxt.strip() == "" or _ITEM_START.match(nxt) or nxt[:1] in ("#", "-"):
                    break
                if nxt[:1] in (" ", "\t"):
                    block.append(nxt)
                    i += 1
                else:
                    break
            blocks.append(("checked" if checked else "unchecked", block))
        else:
            blocks.append(("other", [line]))
            i += 1
    return blocks


def reorder(text: str) -> str:
    lines = text.split("\n")
    blocks = parse_blocks(lines)

    checked = [b for kind, b in blocks if kind == "checked"]
    # Everything else keeps its place, minus any existing Done heading (re-added at the bottom).
    kept: list[str] = []
    for kind, b in blocks:
        if kind == "checked":
            continue
        if kind == "other" and b[0].strip() == DONE_HEADING:
            continue
        kept.extend(b)

    # Collapse runs of blank lines (left behind where items were lifted out) to a single blank.
    collapsed: list[str] = []
    for ln in kept:
        if ln.strip() == "" and collapsed and collapsed[-1].strip() == "":
            continue
        collapsed.append(ln)
    while collapsed and collapsed[-1].strip() == "":
        collapsed.pop()

    if not checked:
        return "\n".join(collapsed) + "\n"

    out = collapsed + ["", DONE_HEADING, ""]
    for b in checked:
        out.extend(b)
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    workspace = pathlib.Path(__file__).resolve().parents[1]
    target = pathlib.Path(argv[1]) if len(argv) > 1 else workspace / "TODO.md"
    raw = target.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    result = reorder(raw.replace("\r\n", "\n"))
    result = result.replace("\n", newline)
    if result == raw:
        print(f"{target.name}: already tidy, nothing to move")
        return 0
    target.write_text(result, encoding="utf-8", newline="")
    print(f"{target.name}: moved checked items to the Done section")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
