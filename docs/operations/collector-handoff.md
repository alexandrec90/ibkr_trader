# Moving the collector between PCs

The collector — the `app` service and its TimescaleDB `db` — runs on one of two Windows
PCs, and the database lives in that PC's `pgdata` volume. Switching PCs moves the database
with it, through the same bucket the [cold-data archive](remote-archive.md) uses: no new
credentials, just the `ARCHIVE_S3_*` keys already in `.env` (`ARCHIVE_BACKEND` is not
consulted, only the bucket and its credentials).

**devkit drives it.** Its `scripts/collectors.py` stops `app` on the source PC, calls this
repo's [`scripts/handoff.py`](../../scripts/handoff.py) to export, and on the idle PC polls
for a pending handoff every 15 minutes, imports it, and starts `app` there. Run by hand,
from the checkout on the host (not in a container):

```bash
docker compose stop app            # on the source PC; db stays up
uv run --extra archive python scripts/handoff.py export --from PC-A --to PC-B
uv run --extra archive python scripts/handoff.py pending --host PC-B   # on PC-B
uv run --extra archive python scripts/handoff.py import --host PC-B    # on PC-B
docker compose --profile app up -d --build app                         # on PC-B
```

`--extra archive` is what brings boto3; a checkout synced without it cannot reach the bucket.

## What each step does

- **export** — `pg_dump -Fc` inside `db`, copied out to a temp file, then uploaded
  (multipart) to `<prefix>/handoff/ibkr_trader/db.dump`. Only once the upload succeeds is
  `marker.json` beside it written: `state: exported`, `from`, `to`, `at`, `key`, `sha256`,
  `bytes`, and the `alembic` revision.
- **pending** — one GET of the marker, no Docker. Prints the marker when it is `exported`
  and addressed to this host; prints nothing otherwise; exits 2 when the bucket cannot be
  read.
- **import** — downloads and checks the dump against the marker's size and sha256, starts
  `db`, stops `app`, drops and recreates the database, and restores it **serially** between
  `timescaledb_pre_restore()` and `timescaledb_post_restore()`, then `ANALYZE`. It never
  starts `app`: that is devkit's step, and `serve --migrate` brings the schema forward on
  start. The marker becomes `state: imported`, `by`, `imported_at`.

The dump object is kept after an import: it is the last handoff's backup, and the next
export overwrites it.

**Never restore this database with `pg_restore -j`.** A parallel restore broke TimescaleDB's
catalog the one time it was done by hand; `tests/test_handoff.py` asserts no `-j` appears.

## When it fails

Every run overwrites `logs/handoff.json` with `command`, `ok`, `exit_code` and `detail` —
on failure too. Exit codes: 0 done, 1 failed (reason in `detail`), 2 cannot tell (only
`pending`, for an unreadable bucket).

- An export that fails writes no marker, so the other PC never sees a half-uploaded dump.
- An import that fails leaves the marker `exported`, so the next poll retries it. The local
  database may be dropped by then; that is safe only because the dump in the bucket is
  intact and verified before anything is dropped.
- `pg_restore` exits 1 on any failed statement. The import accepts that only when every
  error is one a TimescaleDB restore is known to emit harmlessly (`IGNORABLE_RESTORE_ERRORS`
  in the script), and then checks the restored `alembic_version` against the marker and
  that `public` holds tables. Any other error fails the import.
