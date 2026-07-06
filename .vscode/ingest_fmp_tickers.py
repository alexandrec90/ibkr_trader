"""Ingest prices for every symbol listed in a ticker file, via a chosen connector.

This is used by VS Code tasks so verbose connector output and tracebacks can be captured in
artifacts/tasks/ingest-*-prices.log instead of flooding the terminal.

Default source is FMP (tickers.txt, canonical symbols). `--source yahoo` reads Yahoo-style
symbols (e.g. XEQT.TO) — used for symbols FMP's free tier gates; the connector throttles
itself between requests.
"""

from __future__ import annotations

import argparse
import pathlib


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tickers",
        default="tickers.txt",
        help="Path to a newline-delimited ticker file, relative to the workspace.",
    )
    parser.add_argument(
        "--source",
        default="fmp",
        choices=("fmp", "yahoo"),
        help="Which price connector to use for every symbol in the file.",
    )
    return parser.parse_args()


def make_connector(source: str):
    if source == "yahoo":
        from ibkr_trader.ingestion.market.yahoo import YahooConnector

        return YahooConnector()
    from ibkr_trader.ingestion.market.fmp import FmpConnector

    return FmpConnector()


def load_tickers(path: pathlib.Path) -> list[str]:
    tickers: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        symbol = line.strip()
        if symbol and not symbol.startswith("#"):
            tickers.append(symbol)
    return tickers


def main() -> int:
    args = parse_args()
    workspace = pathlib.Path(__file__).resolve().parents[1]
    ticker_path = workspace / args.tickers
    tickers = load_tickers(ticker_path)
    if not tickers:
        print(f"no tickers found in {ticker_path}")
        return 1

    connector = make_connector(args.source)
    failures: list[str] = []
    print(f"source: {args.source}")
    print(f"ticker_file: {ticker_path}")
    print(f"tickers: {', '.join(tickers)}")
    print("")

    for symbol in tickers:
        try:
            count = connector.fetch(symbol=symbol)
        except Exception as exc:
            failures.append(symbol)
            print(f"{symbol}: failed: {type(exc).__name__}: {exc}")
        else:
            print(f"{symbol}: upserted {count} bars")

    if failures:
        print("")
        print(f"failed_tickers: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
