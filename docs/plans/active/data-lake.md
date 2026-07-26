# Shared data-lake plan

## Goal

Turn the data this project gathers (news, social, market bars, features) into a **reusable
data layer** that future data-driven projects — e.g. a sports-betting service that reuses the
news feed — can read without duplicating ingestion or storage architecture. The bulk lives as
Parquet on Cloudflare R2 (S3-compatible, zero egress); local disk stays small.

This is deliberately **not** a "giant data dump." The difference between a lake and a swamp is
a schema + partition convention + a catalog — all of which this repo's `archive/` module
already has (Hive-partitioned Parquet, verify-before-delete, a DuckDB read-only lens), now plus
a self-describing catalog (Phase 1).

## Guardrails that survive the split

- **Bronze, not source of truth.** R2 Parquet is immutable raw/bronze. Postgres stays the
  source of truth for anything `signals/`, `backtest/`, or `execution/` reads. Consumers
  restore-then-use; the hot path never reads the lake directly. (CLAUDE.md conventions.)
- **What goes in the lake:** news, social (authors hashed only — Québec Law 25), market bars,
  derived features. Non-PII, non-account.
- **What never leaves this repo's Postgres:** orders, executions, daily training bars, risk
  state. `archive bars` already refuses `"1 day"`; keep that line.
- **Private bucket, always.** Social payloads are scraped content under Law 25. Never a public
  bucket or public dataset host.
- **IBKR pacing** must move into whatever cloud runner pulls IBKR historical data (Phase 3) —
  it can't rely on the laptop's rate limiter.

## Phases (each = its own session)

| Phase | What | Owner | Status |
|---|---|---|---|
| 0 | Decisions + infra: lake repo name, create **private** R2 bucket + API token, new GitHub repo | You | ☐ pending |
| 1 | **Catalog layer** in this repo's `archive/` module (self-describing manifest per dataset) | Claude | ✅ done — see below |
| 2 | Extract `ingestion/ + archive/ + lens` into a `data-lake` package; `ibkr_trader` depends on it | Claude | ☐ pending — needs Phase 0 + its own Plan |
| 3 | Cloud auto-pull: GitHub Actions (or CF Worker) cron writes Parquet → R2, pacing in the runner | Claude | ☐ pending — needs R2 secrets in CI |
| 4 | Second consumer (sports-betting) reads the lake via the DuckDB lens + catalog | later | ☐ future |

### Phase 1 — done

Added `src/ibkr_trader/archive/catalog.py`:

- `DATASET_SPECS` (`price_bars`, `news_articles`, `social_posts`) — each declares prefix,
  timestamp column, and natural key. A test (`test_dataset_specs_match_the_archive_writers`)
  pins these to `bars._BAR_KEY` / `raw._RAW_KEY` so the catalog can never advertise a wrong key.
- `record_partition(...)` writes/updates `_catalog/<dataset>.json` (natural key, ts column,
  column schema, per-partition rows + min/max ts). Wired into `archive_price_bars` /
  `archive_raw_payloads` right after `verify_partition`, using the merged partition frame.
- `load_manifest` / `load_catalog` / `list_datasets` read the catalog with only `json` (no
  pyarrow needed); `rebuild_catalog` re-derives every manifest by scanning partitions (needs
  the `[archive]` extra) — the backfill/repair path.
- CLI: `ibkr-trader archive catalog [--rebuild]`. `archive status` now excludes `_catalog/`.
- Docs: [remote-archive.md](../../remote-archive.md) "Catalog" section. Tests:
  `tests/test_archive_catalog.py` + a CLI roundtrip in `tests/test_cli.py`.

The catalog is the **reuse contract** Phase 2+ builds on: a foreign project points DuckDB at
the same R2 bucket, reads `_catalog/*.json` to discover datasets/keys/spans, and queries the
partitions — no shared database, no shared code.

### Phase 2 — extraction (next, fresh session)

- New repo `data-lake` (name TBD in Phase 0). Move `ingestion/`, `archive/`, and the DuckDB
  `lens` into an installable package with a stable public API (connectors, `store_from_settings`,
  the catalog readers, restore helpers).
- `ibkr_trader` depends on it; its `signals/`/`backtest/`/`execution/` keep restoring into local
  Postgres. Update this repo's imports, tests, CI, and CLAUDE.md.
- Watch: this repo's test suite + coverage floor shift when `ingestion/`/`archive/` leave. Make
  it one reviewable, revertible commit.

### Phase 3 — cloud auto-pull

- Prefer **GitHub Actions scheduled workflows** (free-tier cron; checks out `data-lake`, runs a
  connector, writes Parquet + updates the catalog straight to R2) so the laptop never holds the
  data. CF Worker Cron Triggers are the lighter-frequency alternative.
- R2 creds as CI secrets. IBKR pacing lives in the runner. Bucket stays private.

## Where this plan lives

This tracked file is the durable handoff. Memory pointer: `data-lake-plan` in the auto-memory
index. When starting a
later phase in a fresh session, read this file first.
