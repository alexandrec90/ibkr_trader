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
| 0 | Decisions + infra: lake repo name, **private** R2 bucket + API token, new GitHub repo | You | ✅ done — bucket `ibkr-trader` wired + reachable, repo [alexandrec90/data-lake](https://github.com/alexandrec90/data-lake) created private (2026-07-29). One open decision + one check below |
| 1 | **Catalog layer** in this repo's `archive/` module (self-describing manifest per dataset) | Claude | ✅ done — see below |
| 1.5 | **Extraction prep** in this repo: models split + config inversion (no new repo needed) | Claude | ✅ done — see below |
| 1.75 | **Session inversion**: connectors take an injected session factory, own no engine | Claude | ✅ done — see below |
| 2 | Extract `ingestion/ + archive/ + lens` into a `data-lake` package; `ibkr_trader` depends on it | Claude | ☐ **unblocked — ready to start in a fresh session** (prep + infra both done) |
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

### Phase 1.75 — session inversion — done

The last hard coupling Phase 1.5 left behind: `ingestion/` called `db.session.get_session()` 11×,
so the connector tree owned this repo's engine. A foreign consumer importing the package would
have had its writes routed through `ibkr_trader`'s `DATABASE_URL`. Now:

- `ingestion/base.py` gained a `SessionFactory` alias (any zero-arg callable returning a context
  manager yielding a committed-on-exit `Session`), `Connector(session_factory=...)`, a lazily
  resolving `Connector.session_factory` property, and `Connector.session()` — the single way a
  connector reaches the database. `resolve_session_factory()` serves the same fallback to the
  module-level batch helpers (`alpha_vantage.fetch_universe`, `finnhub_backfill.run_backfill`,
  `newsapi.fresh_tagged_symbols`, `yahoo_fx._next_missing_date`), which all now take an optional
  `session_factory` and thread it into the connectors they build.
- **No ingestion module names `db.session` any more** — only `base.py`, behind a lazy import
  inside a function. That is the second (and last) line Phase 2 swaps for the consumer's own
  plumbing, next to the config one.
- Connector tests inject the SQLite factory instead of monkeypatching each module's `get_session`
  (`Connector(session_factory=session_cm)`), which is also how a foreign consumer will wire it —
  the tests now exercise the real extraction contract. Mutation-checked: ignoring the injected
  factory fails 15 tests.

Enforced by `tests/test_connector_settings.py` (injection wins, fallback lazy + cached once, the
three batch helpers accept an optional factory, no ingestion module imports `ibkr_trader.db.session`
at any scope, `base.py` has no import-time dependency on config **or** `db.session`) and a new
check in `tests/test_db_models_split.py` that `ingestion/` and `archive/` only ever name lake-side
model classes — importing `Order`/`Execution` there is how the audit trail would follow them into
the shared package. Both were mutation-checked.

Emitted DDL, CLI behaviour and scheduler wiring are unchanged: everything in this repo still
constructs connectors with no arguments and gets `db.session.get_session` by default.

### Phase 2 — extraction (next, fresh session — unblocked)

- New repo **`data-lake`** (name settled in Phase 0). Move `ingestion/`, `archive/`, `db/base.py`,
  `db/lake_models.py`, and the DuckDB `lens` into an installable package with a stable public API
  (connectors, `store_from_settings`, the catalog readers, restore helpers). `db/trading_models.py`
  stays here and keeps importing the package's `Base` + `Instrument`.
- **Prep is complete.** The three couplings are all inverted: models (1.5), config (1.5),
  session factory (1.75). The mechanical work left is the move itself plus two decisions:
  1. **Config surface.** `archive/store.py` and `archive/lens.py` still take
     `ibkr_trader.config.Settings` by type. Replace with a `Protocol` (or a small dataclass) the
     package defines and both repos satisfy — `Settings` already structurally matches, so this is
     an annotation change, not a behaviour change. Do it *in* Phase 2, not before: it only makes
     sense once the package names its own config type.
  2. **Where `ingestion/` imports models from.** It imports the `db.models` façade today (which
     registers every table, so Alembic and SQLite `create_all` stay complete). In the package that
     becomes `data_lake.db.lake_models`; the guardrail test already proves only lake-side classes
     are named, so the rewrite is a path substitution.
- `ibkr_trader` depends on it; its `signals/`/`backtest/`/`execution/` keep restoring into local
  Postgres. Update this repo's imports, tests, CI, and CLAUDE.md.
- Watch: this repo's test suite + coverage floor shift when `ingestion/`/`archive/` leave. Make
  it one reviewable, revertible commit.

### Phase 0 — infra — done (2026-07-29)

- **R2 bucket exists and is wired**: `ARCHIVE_BACKEND=s3`, bucket `ibkr-trader`, R2 endpoint,
  region `auto`, access key + secret all set in `.env` (no prefix). Verified live —
  `uv run --extra archive ibkr-trader archive status` authenticates and lists, returning
  "archive is empty": credentials and connectivity are good, **nothing has been archived yet**, so
  the write path is still unexercised.
- **GitHub repo created**: [alexandrec90/data-lake](https://github.com/alexandrec90/data-lake) —
  private, initialized with a README. Phase 2 clones it and pushes the package there.

Two loose ends, neither blocking Phase 2:

1. **Bucket renamed to `data-lake`** (decided + created 2026-07-29, while still empty so it was
   free). The lake is read by *other* projects (Phase 4's sports-betting consumer), and a
   project-named bucket reads wrong from the consumer side. `ibkr-trader` (created 2026-07-18)
   remains, empty and unused — safe to delete.
   - **Two credential types, easily confused.** The `ARCHIVE_S3_*` keys are S3-style HMAC creds
     (access key id = R2 token id, secret = a one-way hash of the token value) and carry **only**
     that token's scope. The Cloudflare REST API instead wants a Bearer **user API token**
     (My Profile → API Tokens → Custom token; permission `Account · Workers R2 Storage · Edit`).
     The bearer value cannot be recovered from the S3 secret.
   - The original object-scoped token could not create buckets — `ListBuckets` and `CreateBucket`
     both returned `AccessDenied`. **Permission scope, not protocol**: no MCP server or API route
     works around authority the credential lacks.
   - Bootstrap used a short-TTL user API token (`Workers R2 Storage: Edit`) →
     `POST /accounts/{id}/r2/buckets`. Both public-access endpoints work as expected:
     `GET /buckets/{b}/domains/managed` and `GET /buckets/{b}/custom_domains`.
2. **Public access verified disabled — both buckets** (2026-07-29): `r2.dev` managed domain
   disabled, zero custom domains. Nothing was ever exposed. This is a Law 25 matter (scraped social
   content, hashed authors), not a preference — recheck after any dashboard change, and note the S3
   credentials cannot report it; only the REST API can.
   - Both buckets are `location=ENAM` (Eastern North America), `storage_class=Standard`. R2 offers
     no Canada-only jurisdiction, so objects may sit in US-East. Payloads are public news/social
     content with hashed authors, but if strict Québec data residency ever matters, R2 cannot
     provide it — that would mean a different provider, not a different bucket.

### What the lake can actually hold today (measured 2026-07-29)

Worth knowing before Phase 2/4, because it is not what the dataset list implies:

- **`price_bars` will stay empty.** Every stored bar is `"1 day"` (1 682 374 yahoo + 5 300 fmp + 6
  alpha_vantage), and `archive bars` refuses `"1 day"` by design — daily bars are the
  training/backtest input and never leave Postgres. There is no intraday data at all, because the
  IBKR historical connector is still a `TODO(skeleton)`. So `archive bars` is a **no-op** on this
  dataset, and a foreign consumer querying `price_bars/` finds nothing until intraday ingestion
  lands. The catalog's `price_bars` spec is correct but unpopulated.
- **`news_articles` is the real payload dataset**: 282 169 of 282 171 articles are scored with raw
  payloads intact (182 MB of a 1120 MB database), spanning published 2025-07-22 → 2026-07-21.
  `archive raw` offloads the `raw` column only — title, summary, sentiment and hashed author stay in
  Postgres, and `restore-raw` refills for reprocessing.
- **`social_posts` is effectively empty**: 1 post, unscored, so nothing to archive.
- `archive raw`'s only knob is `--min-age-days` on `fetched_at`, and the whole backlog was fetched
  2026-07-15 → 2026-07-29 (the Finnhub backfill), so the grace period is a cliff, not a dial:
  0 → 282 169 payloads, 12 → 135 347, **13 → 1 505**, 14+ → 1 505.

### First real archive run — done, end to end (2026-07-29)

`archive raw --min-age-days 13` against the new `data-lake` bucket, then `archive catalog`:

| stage | result |
|---|---|
| Postgres | 282 171 news rows → 1 505 offloaded (`raw` NULL), 280 666 still local, **0 rows lost title or sentiment** |
| R2 | `raw/news_articles/2026-07.parquet` — 1 object, 0.03 MiB |
| Catalog | `news_articles: 1505 rows in 1 partition, 2026-07-09 → 2026-07-15, key=source+external_id` |
| DuckDB lens | `archive query` reads it straight from R2: 1 505 finnhub payloads, all `raw_json` intact |

That last row is **the Phase 4 reuse contract working for real** — catalog → partition → query, no
shared database and no shared code. The write path, verify-before-delete, the manifest and the
foreign-consumer read path are all now proven against live R2, which is what Phase 2 was waiting
for: the extraction now moves known-working code.

Lens column names are not the Postgres ones — `raw_payloads` exposes `source`, `external_id`,
`fetched_at`, `raw_json` (no `ts`, no `payload`), and `rows` is a DuckDB reserved word.

**Full-backlog run started then interrupted (also 2026-07-29) — and the interruption is instructive.**
`archive raw --min-age-days 0` was stopped part-way (owner shutting the machine down). Final state:

| side | state |
|---|---|
| R2 | 5 partitions, 0.92 MiB — catalog reports 64 777 rows, 2025-07-22 → 2026-07-15 |
| Postgres | **unchanged**: 1 505 offloaded, 280 666 still local, 0 damaged |

The local NULLing runs inside **one transaction that commits at the very end**, so an interrupted run
rolls back every local change while the uploaded partitions stay in R2. That is the safe direction —
those payloads now exist in *both* places, never neither. Rerunning `archive raw --min-age-days 0` is
idempotent: it merges the existing partitions and NULLs locally once verified. Nothing depends on
finishing it; the only cost of the interruption is re-uploading ~0.9 MiB.

Worth knowing for Phase 3: a cloud runner on a job timeout hits exactly this path, so the
all-or-nothing transaction means a long archive job either completes or accomplishes nothing locally.
If that becomes a problem, commit per partition rather than per run.

### Phase 3 — cloud auto-pull

- Prefer **GitHub Actions scheduled workflows** (free-tier cron; checks out `data-lake`, runs a
  connector, writes Parquet + updates the catalog straight to R2) so the laptop never holds the
  data. CF Worker Cron Triggers are the lighter-frequency alternative.
- R2 creds as CI secrets. IBKR pacing lives in the runner. Bucket stays private.

## Where this plan lives

This tracked file is the durable handoff. Memory pointer: `data-lake-plan` in the auto-memory
index. When starting a
later phase in a fresh session, read this file first.
