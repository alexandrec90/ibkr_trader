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
| 0 | Decisions + infra: lake repo name, **private** R2 bucket + API token, new GitHub repo | You | ✅ done 2026-07-29 — bucket `data-lake` live + validated, repo [alexandrec90/data-lake](https://github.com/alexandrec90/data-lake) created private — see below |
| 1 | **Catalog layer** in this repo's `archive/` module (self-describing manifest per dataset) | Claude | ✅ done — see below |
| 1.5 | **Extraction prep** in this repo: models split + config inversion (no new repo needed) | Claude | ✅ done — see below |
| 1.6 | **Session inversion**: `ingestion/` accepts a caller-supplied session factory | Claude | ✅ done — see below |
| 2 | Extract `ingestion/ + archive/ + lens` into a `data-lake` package; `ibkr_trader` depends on it | Claude | ☐ **unblocked — ready for a fresh session** (prep + infra done, write path validated) |
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

### Phase 1.6 — session inversion — done

The last hard coupling Phase 1.5 left: `ingestion/` called `db.session.get_session()` 11×, so an
extracted package would have owned this repo's engine. Inverted with the same shape as the config
fix, so both ambient dependencies now read the same way:

- **`Connector.__init__(settings=None, session_factory=None)`** + a `Connector.session()` helper.
  A session factory is any zero-arg callable returning a context manager that yields a
  SQLAlchemy `Session` and commits on clean exit — `db.session.get_session` is merely the default.
  All 15 in-method call sites became `with self.session() as session:`.
- **Module-level helpers take the same argument**: `alpha_vantage.fetch_universe`,
  `finnhub_backfill.run_backfill`, `newsapi.fresh_tagged_symbols`, `yahoo_fx._next_missing_date`
  resolve theirs through `base.resolve_session_factory(...)`, and the two that build a connector
  hand the factory down instead of letting it fall back.
- **`db/__init__.py` re-exports lazily (PEP 562).** It eagerly imported `db.session`, so *any*
  `import ibkr_trader.db.models` — every connector — pulled in the engine and `config` anyway.
  The AST guard from Phase 1.5 could not see this; a runtime check now does.

Enforced by `tests/test_connector_session_factory.py`: injection wins, the fallback is lazy and
cached, helpers that build a connector propagate the factory, no ingestion module imports
`db.session` (AST), and — the end-to-end one — importing the entire connector tree in a
subprocess loads neither `ibkr_trader.db.session` nor `ibkr_trader.config`. Mutation-checked:
re-adding the import to one connector fails 2 tests, and reverting `db/__init__.py` to eager
imports fails the subprocess check. The existing connector tests now inject a SQLite factory
instead of monkeypatching a module-level `get_session`, which is what a foreign consumer does.

### Phase 0 — infra — done (2026-07-29)

- **Bucket `data-lake`** created (private) and validated end to end. The owner had already created
  an `ibkr-trader` bucket on 2026-07-18; it was renamed-by-recreation while still empty (free), and
  the old empty bucket was deleted. `ARCHIVE_BACKEND=s3`, endpoint, `region=auto`, and an
  **Object Read & Write** token scoped to the one bucket live in `.env`.
- **Repo** [alexandrec90/data-lake](https://github.com/alexandrec90/data-lake) — private,
  README-initialized. Phase 2 clones it and pushes the package there.
- **Public access verified disabled** on both buckets: `r2.dev` managed domain off, zero custom
  domains. Law 25 matter (scraped social content, hashed authors), so recheck after any dashboard
  change. The S3 credentials **cannot** report this — only the REST API or the dashboard can.
- Both buckets were `location=ENAM`, `storage_class=Standard`. R2 offers no Canada-only
  jurisdiction, so objects may sit in US-East; if strict Québec residency ever matters that means a
  different provider, not a different bucket.

Two things that cost time and are easy to repeat:

- **Two credential types, easily confused.** `ARCHIVE_S3_*` are S3-style HMAC creds (access key id =
  R2 token id, secret = a one-way hash of the token value) and carry only that token's scope. The
  Cloudflare REST API instead wants a Bearer **user API token** (My Profile → API Tokens → Custom
  token, permission `Account · Workers R2 Storage · Edit`). The bearer value is **not** recoverable
  from the S3 secret, and R2 bucket creation needs account-level authority: an object-scoped token
  returns `AccessDenied` for both `ListBuckets` and `CreateBucket`. **Permission scope, not
  protocol** — no API route or MCP server works around authority the credential lacks.
- Bucket creation used a short-TTL user API token → `POST /accounts/{id}/r2/buckets`. Public-access
  state comes from `GET /buckets/{b}/domains/managed` + `GET /buckets/{b}/custom_domains`.

### What the lake can actually hold today (measured 2026-07-29)

Not what the dataset list implies:

- **`price_bars` stays empty.** Every stored bar is `"1 day"` (1 682 374 yahoo + 5 300 fmp + 6
  alpha_vantage) and `archive bars` refuses `"1 day"` by design — daily bars are the
  training/backtest input. There is no intraday data because the IBKR historical connector is still
  a `TODO(skeleton)`, so **`archive bars` is a no-op** and a foreign consumer querying `price_bars/`
  finds nothing until intraday ingestion lands. The catalog spec is correct but unpopulated.
- **`news_articles` is the real payload dataset**: 282 169 of 282 171 articles scored with `raw`
  intact (182 MB of a 1120 MB database), published 2025-07-22 → 2026-07-21.
- **`social_posts` is effectively empty**: 1 unscored post.
- `archive raw`'s only knob is `--min-age-days` on `fetched_at`, and the whole backlog was fetched
  2026-07-15 → 2026-07-29 (the Finnhub backfill), so it is a cliff not a dial: 0 → 282 169,
  12 → 135 347, 13 → 1 505.

### First real archive run — validated against live R2 (2026-07-29)

`archive raw --min-age-days 13` then `archive catalog`:

| stage | result |
|---|---|
| Postgres | 1 505 offloaded (`raw` NULL), 280 666 still local, **0 rows lost title or sentiment** |
| R2 | `raw/news_articles/2026-07.parquet` — 0.03 MiB |
| Catalog | `news_articles: 1505 rows in 1 partition, 2026-07-09 → 2026-07-15, key=source+external_id` |
| DuckDB lens | `archive query` reads it back from R2: 1 505 finnhub payloads, `raw_json` intact |

That last row is **the Phase 4 reuse contract working for real** — catalog → partition → query, no
shared database and no shared code. Phase 2 now moves known-working code.

Lens column names are not the Postgres ones: `raw_payloads` exposes `source`, `external_id`,
`fetched_at`, `raw_json` (no `ts`, no `payload`), and `rows` is a DuckDB reserved word.

**The follow-up full run was interrupted, which is instructive.** `archive raw --min-age-days 0` was
stopped part-way. R2 kept 5 partitions (catalog: 64 777 rows) while Postgres rolled back to 1 505
offloaded — because the local NULLing commits in **one transaction at the end of the run**. That is
the safe direction: those payloads exist in *both* places, never neither, and a rerun merges
idempotently. Phase 3 note: a cloud runner hitting a job timeout takes exactly this path, so a long
archive job either completes or accomplishes nothing locally — commit per partition if that bites.

### Phase 2 — extraction (next, fresh session)

- New repo **`data-lake`** (name settled in Phase 0). Move `ingestion/`, `archive/`, `db/base.py`,
  `db/lake_models.py`, and the DuckDB `lens` into an installable package with a stable public API
  (connectors, `store_from_settings`, the catalog readers, restore helpers). `db/trading_models.py`
  stays here and keeps importing the package's `Base` + `Instrument`.
- Config, models and sessions are all inverted now (Phases 1.5 + 1.6) — no coupling left to
  break before the move. Two things still to decide *during* it:
  - `ingestion/` and `archive/` import the `db.models` façade, which registers the trading tables
    too. Post-move they should import `lake_models` directly; the façade's "import it, not the
    halves" rule exists so `Base.metadata` stays complete for Alembic, and only this repo needs
    that.
  - `archive/store.py` and `archive/lens.py` still import `ibkr_trader.config` eagerly (they take
    an optional `Settings`, so it is injection-ready — just not lazy like `ingestion/base.py`).
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
