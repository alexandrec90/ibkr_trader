# Remote cold-data archive

The local box has little disk, so cold data is offloaded to object storage as Parquet and
pulled back on demand. Postgres remains the single source of truth for everything the hot
path reads — the trainer (`signals/`), the backtester, and the audit trail never leave the
DB — and the archive holds only:

| data | archived when | archive layout |
| --- | --- | --- |
| intraday price bars (`price_bars` where `bar_size != "1 day"`) | older than `--older-than-days` (default 365) | `price_bars/bar_size=<slug>/<YYYY-MM>.parquet` |
| `raw` provider payloads on `news_articles` / `social_posts` | sentiment already scored (+ optional `--min-age-days` grace) | `raw/<table>/<YYYY-MM>.parquet` |

Never archived: **daily bars** (the training/backtest input — `archive bars` refuses
`"1 day"` outright), and **orders / executions** (tax + audit trail).

## Safety model

1. Rows are grouped into monthly Parquet partitions and **merged** into any existing
   partition object (idempotent — re-running never duplicates or drops archived rows).
2. The uploaded object is downloaded again and every row's natural key is verified present.
3. Only after verification are local rows deleted (`bars`) or their `raw` NULLed (`raw`).
   If verification fails, the command errors and the local rows are untouched.

`archive raw` is the non-destructive replacement for `prune_scored_raw`: the row (title,
body, sentiment, hashed author) always stays in Postgres; only the payload blob moves, and
`restore-raw` can bring it back for reprocessing.

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
host.

## Commands

```bash
ibkr-trader archive bars --older-than-days 365      # offload old intraday bars
ibkr-trader archive raw --min-age-days 30           # offload scored raw payloads
ibkr-trader archive status                          # list archived objects

# before training an intraday model on 2024 minute bars:
ibkr-trader archive restore-bars --start 2024-01-01 --end 2024-12-31 --bar-size "1 min"
# before re-running sentiment over old payloads:
ibkr-trader archive restore-raw --start 2024-01-01 --end 2024-12-31
```

Restores are idempotent: `restore-bars` inserts only missing bars (recreating instrument
rows by symbol/exchange/currency if the DB was rebuilt); `restore-raw` refills only
currently-NULL payloads and never inserts rows.

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
