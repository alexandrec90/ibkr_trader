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
| 0 | Decisions + infra: lake repo name, create **private** R2 bucket + API token, new GitHub repo | You | ◐ name decided (`data-lake`); bucket + repo still pending |
| 1 | **Catalog layer** in this repo's `archive/` module (self-describing manifest per dataset) | Claude | ✅ done — see below |
| 1.5 | **Extraction prep** in this repo: models split + config inversion (no new repo needed) | Claude | ✅ done — see below |
| 2 | Extract `ingestion/ + archive/ + lens` into a `data-lake` package; `ibkr_trader` depends on it | Claude | ☐ pending — needs Phase 0 infra + its own Plan |
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
- Docs: [remote-archive.md](../../operations/remote-archive.md) "Catalog" section. Tests:
  `tests/test_archive_catalog.py` + a CLI roundtrip in `tests/test_cli.py`.

The catalog is the **reuse contract** Phase 2+ builds on: a foreign project points DuckDB at
the same R2 bucket, reads `_catalog/*.json` to discover datasets/keys/spans, and queries the
partitions — no shared database, no shared code.

### Phase 1.5 — extraction prep — done

Phase 2 as originally written could not honor its own guardrail. `ingestion/` imported
`db.models` 12×, `db.session` 11× and `config` 8×, and **`db/models.py` was one 324-line module
holding all 17 table classes** — orders, executions and strategy snapshots included. Moving
`ingestion/` into a shared package would have dragged the audit-trail table definitions into the
package a foreign consumer imports. Two changes fix that, both in-place and revertible:

- **Models split along the lake seam.** `db/base.py` (`Base`, `SqliteFriendlyBigInt`,
  `JsonVariant`) + `db/lake_models.py` (10 shareable tables) + `db/trading_models.py`
  (5 tables that never leave: `predictions`, `orders`, `executions`, `backtest_runs`,
  `strategy_snapshots`). `db/models.py` is now a re-export façade, so all ~40 existing import
  sites and Alembic's `target_metadata = Base.metadata` are untouched. **Emitted DDL is
  byte-identical to before the split — no migration.** Dependency direction is trading → lake.
- **Config inverted in the connector tree.** `Connector.__init__(settings=None)` +
  a lazily-resolving `Connector.settings` property replaced 8 in-method `get_settings()` calls.
  `ibkr_trader.config` went from 8 hard import-time dependencies to a single lazy import inside
  `ingestion/base.py` — the one line Phase 2 swaps for the consumer's own config.

Enforced by `tests/test_db_models_split.py` (partition is exhaustive/disjoint; audit-trail tables
are never lake-side; `lake_models` may import nothing but `db.base`; `DATASET_SPECS` may only
name lake tables) and `tests/test_connector_settings.py` (injection wins, fallback is lazy and
cached, no ingestion module imports config at module scope). Both guardrails were mutation-checked
— reclassifying `orders` as a lake table fails 3 tests, and a `lake_models → trading_models`
import is caught by the AST check.

`Feature` was placed **lake-side**, matching the plan's "derived features" line above. It has no
FK to any trading table, so this is reversible if you'd rather features stayed private.

### Phase 2 — extraction (next, fresh session)

- New repo **`data-lake`** (name settled in Phase 0). Move `ingestion/`, `archive/`, `db/base.py`,
  `db/lake_models.py`, and the DuckDB `lens` into an installable package with a stable public API
  (connectors, `store_from_settings`, the catalog readers, restore helpers). `db/trading_models.py`
  stays here and keeps importing the package's `Base` + `Instrument`.
- Still to invert before the move: `ingestion/` calls `db.session.get_session()` 11× (the package
  must accept a caller-supplied session factory rather than owning the engine). That is the one
  remaining hard coupling; Phase 1.5 cleared config and the models.
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
