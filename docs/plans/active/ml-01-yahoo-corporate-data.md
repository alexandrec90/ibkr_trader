# Plan ML-01 — Yahoo corporate data ingestion (dividends, shares, sector, statements)

Read first: [README.md](../README.md) · [CLAUDE.md](../../../CLAUDE.md) ·
[yahoo.py](../../../src/ibkr_trader/ingestion/market/yahoo.py) ·
[test_yahoo_connector.py](../../../tests/test_yahoo_connector.py) ·
[db/models.py](../../../src/ibkr_trader/db/models.py)

## Context

The feature pipeline (plan ML-02) needs corporate data beyond OHLCV. A live probe of free
yfinance (2026-07) established what's actually served:

| Data | yfinance surface | Depth | Use |
|---|---|---|---|
| Dividends | `Ticker.dividends` | decades | trailing yield, dividend growth features |
| Share counts | `Ticker.get_shares_full(start=...)` | ~2015+ | historical market cap (size feature) |
| Sector/industry/name | `Ticker.info` | current only | instrument metadata (static-ish) |
| Statements | `Ticker.income_stmt` / `balance_sheet` / `cashflow` (+ `quarterly_*`) | **~4–5 annual periods, ~5–7 quarters** | fundamentals ratios (feature-set v2) |
| Earnings report dates | `Ticker.get_earnings_dates(limit=...)` | back to ~2001 | point-in-time lagging of statements |

ETFs return nothing beyond dividends — skip statements for `asset_class == "ETF"` gracefully.
Statements are shallow, so the design goal is **snapshot forward**: every quarterly ingest run
upserts the latest statements, and a `first_seen` timestamp records when each figure entered
our DB, so future feature builds can honestly answer "what did we know at time t?".

## Deliverables

1. **Schema** (models + one Alembic migration, reviewed before upgrade):
   - `Instrument`: add `sector: str | None` and `industry: str | None` (String columns).
   - `dividends`: `instrument_id` FK, `ex_date` (Date), `amount` (Float), `source` (String),
     unique `(instrument_id, ex_date, source)`.
   - `share_counts`: `instrument_id` FK, `date` (Date), `shares` (Float), `source`,
     unique `(instrument_id, date, source)`.
   - `fundamental_snapshots`: `instrument_id` FK, `freq` (`annual`|`quarterly`),
     `statement` (`income`|`balance`|`cashflow`), `period_end` (Date), `payload` (JSONB —
     line-item name → value), `report_date` (Date, nullable — from earnings dates when
     matchable), `first_seen` (UTC DateTime, set on insert, **never updated**),
     `fetched_at` (UTC DateTime, updated each refresh);
     unique `(instrument_id, freq, statement, period_end)`.
   - `earnings_events`: `instrument_id` FK, `report_ts` (UTC DateTime), `source`,
     unique `(instrument_id, report_ts, source)`.
   - All new PKs use the existing `SqliteFriendlyBigInt` pattern.
2. **Connector** `src/ibkr_trader/ingestion/market/yahoo_fundamentals.py` implementing
   `base.Connector`. Reuse yahoo.py's symbol mapping and its module-level throttle (refactor
   `_throttle`/`_instrument_defaults` into a shared helper rather than duplicating; one
   `Ticker` object per symbol, throttle before each distinct yfinance call). `fetch(symbol=...)`
   upserts all five data kinds for one symbol and returns a row count. Point-in-time rule:
   when a statement period's `report_date` can be inferred (nearest earnings event after
   `period_end`, within ~120 days), store it; downstream availability is
   `report_date or first_seen`.
3. **CLI**: `ibkr-trader ingest fundamentals SYMBOL` (mirror `ingest prices` conventions) and
   a batch path via `.vscode/ingest_fmp_tickers.py` (add `--source yahoo-fundamentals` or a
   sibling script) + a VS Code task `ingest: yahoo fundamentals (tickers-yahoo.txt)`.
4. **Tests** (pattern: stub the yfinance download functions exactly like
   `test_yahoo_connector.py` does — no network in tests): upsert idempotency (re-fetch
   doesn't duplicate, `first_seen` survives refresh), ETF graceful skip, report-date
   matching, sector metadata written.
5. Update [docs/reference/data-sources.md](../../reference/data-sources.md) row for Yahoo with the fundamentals
   depths above, and tick this plan off in TODO.md §1.

## Out of scope

Feature computation (ML-02), any FMP fundamentals, SEC EDGAR, news.

## Acceptance checklist

- [ ] Migration applied on dev DB (port 5433); autogen reviewed by hand
- [ ] `ingest fundamentals AAPL` then `ingest fundamentals RY.TO` populate all five tables
- [ ] Second run of the same command inserts nothing new (idempotent), `first_seen` unchanged
- [ ] XEQT.TO run stores dividends only, no error
- [ ] pytest / ruff / mypy green
