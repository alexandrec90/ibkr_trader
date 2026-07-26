# Plan TOOLS-06 — TimescaleDB compression for price_bars (measure first, may abort)

**Status: implemented 2026-07-19.** Phase 0 (2026-07-18) measured the hot window as tiny and
deferred the plan; the owner then elected to proceed ahead of a planned substantial increase in
retained data — the exact "re-run this gate if the hot relation grows" condition Phase 0 named.
The Phase 0 abort record is kept below as history; see **Phase 1 — implemented** for what shipped.

Read first: [README.md](../README.md) · [CLAUDE.md](../../../CLAUDE.md) ·
[docker-compose.yml](../../../docker-compose.yml) ·
[db/models.py](../../../src/ibkr_trader/db/models.py)
(`PriceBar`) · [docs/remote-archive.md](../../remote-archive.md) (the existing disk-pressure
answer) · `migrations/` (how manual `op.execute` migrations are written here)

## Context

TimescaleDB is a Postgres extension: `price_bars` becomes a hypertable with native columnar
compression (~90% on OHLCV), attacking disk pressure in the HOT window while the SQLAlchemy
models, Alembic stack, and every query survive untouched. It is infrastructure-heavy (new
Docker image, data migration) and the archive layer already offloads cold intraday bars —
so this plan **starts with a measurement gate and is expected to abort if the numbers don't
justify it**.

## Phase 0 — measurement gate (do this before any infra change)

Against the dev DB (port **5433**, `docker compose exec db psql ...` — note the postgres MCP
in this workspace points at the wrong DB, don't trust it):

- `pg_total_relation_size` for `price_bars` (and top tables) + total DB size.
- Row counts by `bar_size` and age; how much is inside vs outside the archive threshold.
- Projected growth (rows/day from the scheduler's ingest cadence).

**Abort criterion:** if `price_bars` HOT-window data is under a few GB and projected growth
is modest, stop — write the numbers into this file, mark the plan "deferred: not
justified", and be done. The archive already solves cold storage; Timescale only earns its
complexity when the *hot* window itself hurts.

### Phase 0 result — abort

Measured against the running Compose dev database on host port 5433 at
`2026-07-18 22:54:07 UTC`. Sizes are PostgreSQL's `pg_size_pretty` output; row counts below
are exact `count(*)` results rather than the stale estimates in `pg_stat_user_tables`.

| Measurement | Result |
|---|---:|
| Total database | 737 MB (772,496,407 bytes) |
| `price_bars` total relation | 399 MB (418,717,696 bytes) |
| `price_bars` heap | 202 MB (212,008,960 bytes) |
| `price_bars` indexes | 197 MB (206,626,816 bytes) |
| `price_bars` rows | 1,668,305 |
| Next-largest table, `news_articles` | 328 MB (343,523,328 bytes) |
| Largest remaining table, `trend_points` | 736 kB |

Every stored price bar is currently a `1 day` bar; there are no intraday bars in the dev
database. The age/archive split is therefore:

| Storage class | Rows | Oldest | Newest |
|---|---:|---:|---:|
| Daily bars, retained regardless of age | 1,668,305 | 1962-01-02 | 2026-07-18 |
| Of those daily bars, inside 365 days | 47,794 | — | — |
| Of those daily bars, outside 365 days | 1,620,511 | — | — |
| Intraday hot window (inside 365 days) | 0 | — | — |
| Intraday archive-eligible (outside 365 days) | 0 | — | — |

The scheduled price job runs once every 24 hours. It currently tracks 187 non-FX Yahoo
series plus one FX pair from both Yahoo and FMP, for approximately 189 new rows per active
market date. The last 30 calendar days contain 3,875 dated bar rows (129.17 rows/day,
or about 47,146/year). A conservative cadence-based projection using 252 equity market days
and 365 FX dates is similarly modest at about 47,854 rows/year. At the current total-relation
density of approximately 251 bytes per row, that is roughly 12 MB/year of additional table
and index storage. Even treating all bars from the last 365 days as the hot window yields
only about 12 MB on the same proportional basis, far below the "few GB" gate.

**Verdict: abort and defer TimescaleDB.** The existing table is under 0.4 GB, the actual hot
window is tiny, growth is modest, and there is no intraday data for the archive/compression
split to optimize. A best-case 90% reduction of the entire current relation would save only
about 360 MB. That does not justify replacing the database image, backing up/restoring the
volume, introducing a Timescale-only migration path, or accepting compressed-chunk upsert
complexity. No Compose, migration, model, query, or archive-documentation changes were made.
Re-run this gate if the service begins retaining intraday bars or the hot relation approaches
multiple gigabytes.

## Phase 1 — implemented (2026-07-19)

Proceeded because the owner intends to grow retained data substantially. Executed against the
Compose dev DB (host port 5433) with a full `pg_dump` backup taken first.

**What shipped:**

- **Image:** `docker-compose.yml` `db` swapped `postgres:16` → `timescale/timescaledb:2.17.2-pg16`
  (pinned, not `latest`). Drop-in Postgres 16; behaves identically until `CREATE EXTENSION`.
- **Migration** `f7b8c9d0e1f2_timescaledb_price_bars_hypertable.py` (manual `op.execute`),
  **guarded** on `pg_available_extensions` so it is a clean no-op on SQLite and on plain
  Postgres — tests/CI never require the extension. On Timescale it: widens the PK to
  `(id, ts)` (the only blocker — a hypertable needs the partition column in every unique/PK
  constraint; the existing `(instrument_id, ts, bar_size, source, what_to_show)` unique key and
  `(instrument_id, ts)` index already include `ts`), then `create_hypertable(by_range('ts',
  INTERVAL '1 month'), migrate_data => true)`, sets compression (`segmentby = instrument_id,
  bar_size`; `orderby = ts DESC`), and `add_compression_policy(… INTERVAL '7 days')`.
- **Model:** `PriceBar` keeps `id`-only PK in the ORM (SQLite autoincrement + Alembic does not
  diff PKs → no autogenerate drift); a comment documents the Postgres composite PK.
- **Docs:** hot-storage section + backup/rebuild procedure added to `docs/remote-archive.md`.
- **Tests:** `tests/test_timescaledb_migration.py` — SQLite no-op (up + down) and guard logic
  (non-Postgres, plain-Postgres, Timescale-present). No test requires Timescale.

**Measured on the dev DB (1,686,305-row `price_bars`, all `1 day` bars, 787 chunks):**

| Measurement | Result |
| --- | ---: |
| `price_bars` total relation, before migration | 398 MB |
| `hypertable_size` after full compression | 267 MB (~33% smaller) |
| Compressed-data bytes (Timescale stats) | 515 MB → 242 MB (**53.1%** saved) |
| Chunks compressed | 786 / 787 (newest stays hot, < 7 days) |
| Total database size after | 494 MB (was 737 MB pre-migration snapshot) |

The realized ratio (~53% on data) is below the aspirational ~90% because these are daily OHLCV
bars segmented per instrument (many small segments) rather than dense single-series intraday
data; it will improve as intraday retention grows. The background compression policy ran on its
own within seconds of the migration, confirming the policy end to end.

**Verified live (dev DB):** UPDATE of an existing row in a compressed chunk succeeds (auto
decompress); a duplicate INSERT is still rejected by the unique key (upsert idempotency intact);
a new INSERT into a compressed time range succeeds and reads back; a full `alembic upgrade head`
from a fresh empty Timescale database converts `price_bars` to a hypertable with PK `(id, ts)`.

**Gate:** `pytest` 424 passed (coverage 90.59% ≥ 90.5% floor), `ruff check`, `ruff format
--check`, `mypy` (61 files) all green.

## Decisions already made (if Phase 0 passes)

- Image: `timescale/timescaledb:latest-pg16` (match current major; check compose) — dev DB
  only. Volume is preserved or rebuilt via dump/restore; **back up first**
  (`pg_dump` to a dated file) and say so in the session.
- Hypertable conversion is a **manual Alembic migration** (`op.execute`), guarded so it
  no-ops on SQLite and on Postgres without the extension (tests + fresh envs must not
  break): `create_hypertable('price_bars', by_range('ts'), migrate_data => true)`,
  chunk interval ~1 month.
- Compression policy: segment by `instrument_id, bar_size`, order by `ts`; compress chunks
  older than ~7 days. Uncompressed recent chunks keep upserts cheap; note that Timescale
  compressed chunks support upserts via decompression but the idempotent
  (source, external_id)-style upsert path for bars must be re-tested explicitly.
- Models/queries unchanged. `daily` bars and orders/executions policy from CLAUDE.md is
  unaffected (they stay in Postgres either way — this changes their storage, not location).

## Deliverables

- Phase 0 numbers appended to this file + go/no-go verdict.
- If go: compose change, backup procedure documented in `docs/remote-archive.md` (it's the
  storage doc), the guarded migration, compression policy, and before/after size numbers.

## Testing (mandatory if go)

- Migration no-ops cleanly on SQLite (full test suite green untouched).
- On the Timescale dev DB: ingest upsert idempotency re-run against a compressed-chunk
  range; backtest engine reads a compressed range correctly; `alembic upgrade head` from a
  fresh volume works.
- No test may require Timescale — CI/postgres-less environments stay green.

## Out of scope

- Continuous aggregates, retention policies (the archive owns deletion), prod/cloud sizing.

## Done when

- [x] Phase 0 numbers recorded; explicit go/abort verdict written here (2026-07-18: abort;
  2026-07-19: reversed on the owner's planned data growth — see Phase 1)
- [x] (if go) dev DB on Timescale, compression active, size delta recorded — 398 MB relation →
  267 MB hypertable, 53.1% on compressed data (see Phase 1)
- [x] (if go) Full gate green: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src` — 424 passed, coverage 90.59%, ruff + mypy clean
