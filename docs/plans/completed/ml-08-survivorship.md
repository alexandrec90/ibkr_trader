# Plan ML-08 — Survivorship bias: label it, size it, plan the fix

Read first: [README.md](../README.md) · [CLAUDE.md](../../../CLAUDE.md) ·
[backtest/engine.py](../../../src/ibkr_trader/backtest/engine.py) ·
[data-sources.md](../../reference/data-sources.md) · [TODO.md](../../../TODO.md) §3
(open survivorship item)

## Context

The universe ([tickers.txt](../../../tickers.txt), ~180 names) was curated in 2025/26 from
today's TSX 60 / S&P 100 / CAD ETFs — every name in it, by construction, survived. Names that
delisted, cratered, or were acquired never enter a backtest, which inflates **every**
strategy's numbers and selection-heavy ones (momentum, `ml_lt`) most. IBKR provides no
delisted-ticker history, so the full fix needs an external data decision by the owner. This
plan makes the bias impossible to forget, bounds what can be said despite it, and prepares
that decision — it deliberately contains little code.

## Deliverables

1. **Label every run (closes the TODO §3 item):** stamp
   `universe: {source, n_symbols, sha256_16, survivorship: "curated-current"}` into
   `backtest_runs.params` (reuse `train.py`'s `_universe_hash`), print a one-line warning in
   `backtest run` output and a footer note in `backtest compare`. Tests for both.
2. **Interpretation rule, written down:** in
   [registered-account-strategy.md](../../registered-account-strategy.md), document that absolute
   excess returns from this universe are **upper bounds**, and that strategy-vs-strategy
   ranking on the identical universe is the defensible comparison. One paragraph, no hedging.
3. **Data-source research (no purchases, no code):** evaluate delisted-inclusive daily-bar
   sources covering US + TSX — at minimum Norgate Data, Sharadar/Nasdaq Data Link, EODHD,
   FMP's paid tiers — for delisting coverage, TSX support, price, licensing for personal use.
   Record findings + a recommendation in [data-sources.md](../../reference/data-sources.md). The owner
   decides whether to buy; do not subscribe to anything in-session.
4. **Ingestion readiness note:** confirm the schema can hold dead tickers (instruments with an
   end-of-life date, bars that simply stop) and note any gaps as TODO items — implementation
   waits for the chosen source.

## Out of scope

Buying/ingesting any delisted dataset (follow-up plan once the owner picks a source),
point-in-time index reconstitution, changes to strategies.

## Acceptance checklist

- [x] Runs and compare output carry the survivorship label; params stamped; tests green
- [x] Upper-bound interpretation documented in the strategy doc
- [x] Source comparison + recommendation written into data-sources.md
- [x] pytest / ruff / mypy green
