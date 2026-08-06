# Remote cold-data archive

> **Where the code lives:** since Phase 2 of the [data-lake plan](../plans/active/data-lake.md)
> the archive is `data_lake.archive`, in the sibling
> [`data-lake`](https://github.com/alexandrec90/data-lake) checkout — not this repo. The
> `ibkr-trader archive …` commands below are unchanged; they are this repo's CLI over that
> package. Fix archive behaviour in `../data-lake`, and configure it through the same `.env`
> keys (`Settings` is handed to the package by `src/ibkr_trader/lake.py`).

The local box has little disk, so cold data is offloaded to object storage as Parquet and
pulled back on demand. Postgres remains the single source of truth for everything the hot
path reads — the trainer (`signals/`), the backtester, and the audit trail never leave the
DB — and the archive holds only:

| data | archived when | archive layout |
| --- | --- | --- |
| intraday price bars (`price_bars` where `bar_size != "1 day"`) | older than `--older-than-days` (default `ARCHIVE_BARS_OLDER_THAN_DAYS`, 90) | `price_bars/bar_size=<slug>/<YYYY-MM>.parquet` |
| `raw` provider payloads on `news_articles` / `social_posts` | sentiment already scored (+ `--min-age-days` grace, default `ARCHIVE_RAW_MIN_AGE_DAYS`, 30) | `raw/<table>/<YYYY-MM>.parquet` |

Never archived: **daily bars** (the training/backtest input — `archive bars` refuses
`"1 day"` outright), and **orders / executions** (tax + audit trail).

## Hot storage: TimescaleDB compression for `price_bars`

The archive above offloads *cold* data. The *hot* window that stays in Postgres is kept small
by TimescaleDB: `price_bars` is a **hypertable** partitioned on `ts` (~1 month chunks) with
native columnar compression (`compress_segmentby = instrument_id, bar_size`,
`compress_orderby = ts DESC`). A policy compresses chunks older than **7 days**; the most
recent chunk stays row-oriented so the read-then-write bar upsert stays cheap. Timescale
transparently decompresses older chunks on write, so upserts and same-day corrections into a
compressed range still work and the `(instrument_id, ts, bar_size, source, what_to_show)`
uniqueness is still enforced.

This is transparent to the app: the SQLAlchemy models, queries, backtester, and archive
commands are unchanged. The one Postgres-only detail is that the migration widens the
`price_bars` primary key to `(id, ts)` (a hypertable needs the partition column in every
unique/PK constraint); the ORM still declares `id` alone, which is what SQLite tests need. On
plain Postgres or SQLite the schema migration is a **no-op**, so tests and CI never require
the extension.

The dev database image is `timescale/timescaledb:2.17.2-pg16` (see `docker-compose.yml`) — a
drop-in Postgres 16; without `CREATE EXTENSION timescaledb` it behaves exactly like
`postgres:16`.

### Backing up / rebuilding the DB volume

The extension lives in the image, so switching images (or upgrading Postgres major versions)
means dumping and restoring the `pgdata` volume. **Always `pg_dump` to a dated file first:**

```bash
# 1. quiesce writers and back up (plain SQL, portable across the image swap)
docker compose stop app
docker compose exec -T db pg_dump -U trader -d ibkr_trader --no-owner --no-privileges \
  > "pgdata_backup_$(date +%Y%m%d_%H%M%S).sql"

# 2. swap the image (edit docker-compose.yml) and recreate the volume from empty
docker compose stop db && docker compose rm -f db
docker volume rm ibkr_trader_pgdata
docker compose up -d db          # waits healthy on the new image

# 3. restore, then let migrations convert price_bars into a hypertable
docker compose exec -T db psql -U trader -d ibkr_trader < pgdata_backup_YYYYMMDD_HHMMSS.sql
uv run alembic upgrade head
docker compose start app
```

The dump is plain Postgres (no Timescale objects), so it restores cleanly onto either image;
`alembic upgrade head` then applies the hypertable/compression migration when the extension is
present. Keep backups out of git (they hold the full DB) — write them to a scratch/external
path, never the repo.

## Safety model

1. Rows are grouped into monthly Parquet partitions and **merged** into any existing
   partition object (idempotent — re-running never duplicates or drops archived rows).
2. The uploaded object is downloaded again and every row's natural key is verified present.
3. Only after verification are local rows deleted (`bars`) or their `raw` NULLed (`raw`).
   If verification fails, the command errors and the local rows are untouched.

`archive raw` is the non-destructive replacement for `prune_scored_raw`: the row (title,
body, sentiment, hashed author) always stays in Postgres; only the payload blob moves, and
`restore-raw` can bring it back for reprocessing.

> **Archiving does not shrink the database file — vacuum it afterwards.** NULLing a `raw` blob
> writes a new row version and leaves the old one dead; the space becomes reusable but is not
> returned to the filesystem, so the database *grows*. Autovacuum reuses it for future inserts,
> which is fine on a growing table and needs no action. To actually reclaim the disk:
>
> ```bash
> docker compose exec db psql -U trader -d ibkr_trader -c "VACUUM (FULL, ANALYZE) news_articles;"
> ```
>
> Measured 2026-07-30, after offloading 280 667 payloads:
>
> | | database | `news_articles` total | heap |
> | --- | --- | --- | --- |
> | after `archive raw` | 1299 MB | 361 MB | 315 MB |
> | after `VACUUM FULL` | **1099 MB** | **161 MB** | 138 MB |
>
> It took **5 seconds**, not the minutes the exclusive lock implies — the rewrite is cheap once
> the payloads are gone. Row count, titles and sentiments were unchanged (282 171 / 0 / 0).
> `VACUUM FULL` still takes an **ACCESS EXCLUSIVE** lock and rewrites the table into new files,
> so it needs roughly the table's size in free space and must not run while `serve` is writing.
> `pg_repack` is the online alternative if that ever matters.
>
> Do **not** point `VACUUM FULL` at `price_bars`: it is a TimescaleDB hypertable whose chunks
> are compressed, and it now dominates the database (~900 MB of the 1099 MB). Timescale's own
> compression policy is what manages that space.

**One run = one transaction.** The local NULLing commits once, at the end, after every
partition has uploaded and verified. An interrupted run therefore leaves the partitions in the
bucket and Postgres completely untouched — payloads exist in both places, never neither, and a
rerun merges idempotently. The run also holds the whole batch in memory (~1.1 GB RSS for
280 k payloads) and took ~2 h. Committing per partition would fix both, and Phase 3's cloud
job timeouts will need it.

## Setup

Install the extra and configure a backend in `.env` (see `.env.example`):

```bash
uv sync --extra archive
```

- `ARCHIVE_BACKEND=s3` — any S3-compatible service. Cloudflare R2 (10 GB free, **zero
  egress fees** — retraining on archived data costs nothing) and Backblaze B2 (10 GB free)
  are the intended targets: create a bucket + an API token, then set
  `ARCHIVE_S3_BUCKET`, `ARCHIVE_S3_ENDPOINT_URL`, `ARCHIVE_S3_ACCESS_KEY_ID`,
  `ARCHIVE_S3_SECRET_ACCESS_KEY` (R2 additionally wants `ARCHIVE_S3_REGION=auto`).
- `ARCHIVE_BACKEND=local` — a plain directory (`ARCHIVE_LOCAL_DIR`), e.g. an external
  drive; also handy for a dry run before pointing at a bucket.

**The bucket must be private.** Social payloads are scraped content stored under Québec
Law 25 constraints (authors hashed only) — never a public bucket, never a public dataset
host. R2 buckets are private by default, but an enabled `r2.dev` managed domain or a custom
domain makes objects public, and **the S3 credentials cannot report that** — only the
Cloudflare REST API or the dashboard can (R2 → bucket → Settings → Public access).

> **Gotcha: a shell environment variable silently outranks `.env`.**
> `Settings` is pydantic-settings, which ranks the process environment *above* the `.env`
> file. If `ARCHIVE_S3_BUCKET` (or any other key) is exported in the shell — e.g. left over
> from an earlier `source .env`, inherited from the terminal that launched your editor — then
> editing `.env` changes nothing and the CLI keeps talking to the old bucket, with no warning.
> This cost real debugging time on 2026-07-29: `archive status` returned `AccessDenied` while a
> direct boto3 call to the same-named bucket succeeded, because the two were reading different
> bucket names. When a config change appears to be ignored, check first:
>
> ```bash
> env | grep ARCHIVE_          # anything here wins over .env
> ```
>
> Fix it at the source (restart the shell/editor session, or correct whatever exports them);
> `VAR=value uv run …` is only a per-command workaround.

## Running it on a schedule

`serve` registers two daily jobs — `archive_bars` and `archive_raw` — that run the same
offloads as the CLI, using `ARCHIVE_BARS_OLDER_THAN_DAYS` and `ARCHIVE_RAW_MIN_AGE_DAYS`.
They are what keeps the local database from growing back; the CLI commands below remain the
way to do a one-off or a different window.

Both jobs **no-op while `ARCHIVE_BACKEND=none`**, so they are inert on a default install. They
are still registered in that case, deliberately: `job_health` seeds itself from the previous
run's artifact, so a job that stops being registered keeps its recorded cadence and is
reported `stale` forever after.

> **Drain the backlog once from the CLI before relying on the schedule.** A run holds the
> whole batch in memory in a single transaction — measured ~1.1 GB RSS and ~2 h for the
> initial 280 k payloads. That is why neither job fires at startup. Once the backlog is
> drained each daily run only has a day of new rows to move, which is cheap.

Cadence knobs: `ARCHIVE_BARS_HOURS`, `ARCHIVE_RAW_HOURS` (both 24 by default). Job outcomes
land in the scheduler health artifact like every other job — `ibkr-trader health` reads it.

## Commands

```bash
ibkr-trader archive bars                            # offload intraday bars past the window
ibkr-trader archive raw                             # offload scored raw payloads
ibkr-trader archive bars --older-than-days 365      # ...or override the window for one run
ibkr-trader archive status                          # list archived data objects
ibkr-trader archive catalog                         # summarize datasets from the catalog
ibkr-trader archive catalog --rebuild               # recompute the catalog from partitions

# before training an intraday model on 2024 minute bars:
ibkr-trader archive restore-bars --start 2024-01-01 --end 2024-12-31 --bar-size "1 min"
# before re-running sentiment over old payloads:
ibkr-trader archive restore-raw --start 2024-01-01 --end 2024-12-31
```

Restores are idempotent: `restore-bars` inserts only missing bars (recreating instrument
rows by symbol/exchange/currency if the DB was rebuilt); `restore-raw` refills only
currently-NULL payloads and never inserts rows.

## Catalog: a self-describing manifest per dataset

Partitions follow stable prefixes, but knowing *what* is in the bucket otherwise means
reading code for the naming convention. The catalog closes that gap: alongside the data,
under `_catalog/<dataset>.json`, each dataset carries a small JSON manifest describing its
natural key, its timestamp column, its column schema, and per-partition row counts and time
spans. Anything with read access to the bucket can discover the contents from the manifests
alone — no Postgres, no code. This is what lets a **separate project share the same bucket**:
the catalog is the reuse contract (see
[shared data-lake plan](../plans/active/data-lake.md)).

The datasets today are `price_bars`, `news_articles`, and `social_posts`. Manifests are
written automatically as `archive bars` / `archive raw` upload and verify each partition, so
they stay in step with the data. They are metadata, never a source of truth — derived from
the partitions and rebuildable from them at any time:

```bash
ibkr-trader archive catalog            # print each dataset: rows, partitions, span, key
ibkr-trader archive catalog --rebuild  # re-read every partition and regenerate the manifests
```

Use `--rebuild` to backfill a bucket that was archived before catalogging existed, or after a
manual object change. Reading the catalog needs only the standard install; `--rebuild` re-reads
the Parquet and so needs the `[archive]` extra. Manifests live under `_catalog/` and are
excluded from `archive status` (which reports data objects only).

## Exploring the archive with DuckDB

Install the research lens alongside the archive tools:

```bash
uv sync --extra archive --extra research
```

The lens exposes two views over the configured local or S3/R2 archive without restoring
data: `bars` has `symbol`, `exchange`, `currency`, `ts`, `bar_size`, `source`,
`what_to_show`, OHLC, and `volume`; `raw_payloads` has `source`, `external_id`, `event_ts`,
`fetched_at`, and `raw_json`.

```bash
ibkr-trader archive query \
  "SELECT ts, close FROM bars WHERE symbol = 'XEQT.TO' AND ts >= '2025-01-01' ORDER BY ts"

# CSV for a pipe or research script
ibkr-trader archive query --csv \
  "SELECT source, count(*) AS payloads FROM raw_payloads GROUP BY source"
```

DuckDB scans the Parquet files in place and pushes column and predicate filters into the
scan. The connection is in memory, attaches no writable database, and the CLI accepts one
`SELECT` statement only. It is a read-only research lens, not a second system of record.

Anything that feeds signals, model training, backtests, or execution must still go through
`archive restore-bars` or `archive restore-raw` first. Those paths remain Postgres-only by
design; never import the lens from `signals/`, `backtest/`, or `execution/`.
