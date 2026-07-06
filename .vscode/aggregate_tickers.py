"""Aggregate the per-connector ingest lists into tickers.txt (the backtest universe).

`backtest run --universe-file` reads tickers.txt by default and the engine resolves each line
by bare instrument symbol, so Yahoo-style suffixes are stripped here the same way the yahoo
connector does (XEQT.TO -> XEQT). Two things this script guards:

- cli.py's universe reader does NOT skip `#` comments, so tickers.txt is written comment-free;
- the engine's symbol lookup can't disambiguate cross-listings (TSX K.TO vs US K), so if the
  source lists ever collapse two different entries onto one canonical symbol, warn loudly.
"""

from __future__ import annotations

import pathlib

from ingest_fmp_tickers import load_tickers

SOURCE_FILES = ("tickers-fmp.txt", "tickers-yahoo.txt")
OUTPUT_FILE = "tickers.txt"


def canonical(symbol: str) -> str:
    return symbol.strip().upper().removesuffix(".TO")


def main() -> int:
    workspace = pathlib.Path(__file__).resolve().parents[1]
    first_seen: dict[str, str] = {}  # canonical -> raw entry that claimed it
    collisions: list[str] = []
    for name in SOURCE_FILES:
        for raw in load_tickers(workspace / name):
            raw = raw.upper()
            key = canonical(raw)
            previous = first_seen.setdefault(key, raw)
            if previous != raw:
                collisions.append(f"{key}: {previous!r} vs {raw!r}")

    symbols = sorted(first_seen)
    output = workspace / OUTPUT_FILE
    output.write_text("\n".join(symbols) + "\n", encoding="utf-8")
    print(f"wrote {len(symbols)} symbols to {output}")

    if collisions:
        print("")
        print("WARNING: entries from different lists collapse to the same bare symbol,")
        print("and the backtest engine resolves universe lines by bare symbol only:")
        for line in collisions:
            print(f"  {line}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
