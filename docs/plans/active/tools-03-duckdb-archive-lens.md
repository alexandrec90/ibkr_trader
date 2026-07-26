# Plan TOOLS-03 — DuckDB read-only lens over the Parquet archive (+ Polars convention)

Read first: [README.md](../README.md) · [CLAUDE.md](../../../CLAUDE.md) ·
[docs/remote-archive.md](../../remote-archive.md) ·
[archive/store.py](../../../src/ibkr_trader/archive/store.py) ·
[archive/parquet_io.py](../../../src/ibkr_trader/archive/parquet_io.py)
(key layout / partitioning) ·
[config.py](../../../src/ibkr_trader/config.py) (`archive_*` settings, ~lines 105–112) ·
[cli.py](../../../src/ibkr_trader/cli.py) (`archive_app`, `_archive_store`)

## Context

Cold data (intraday bars, scored raw payloads) lives as Parquet in object storage (local dir
or S3/R2). Today the only way to look at it is `restore-*` back into Postgres. DuckDB is an
in-process columnar engine that scans Parquet — including `s3://` with predicate pushdown —
spectacularly well. It is single-writer with a weak multi-process story, which is exactly why
it is **not** a Postgres replacement here: it becomes a read-only research lens.

**Hard boundary (the point of this plan):** nothing in `signals/`, `backtest/`, or
`execution/` may import the lens. Anything feeding the real pipeline goes through
`archive restore-*` into Postgres first. Postgres stays the system of record.

## Decisions already made

- New optional extra `research = ["duckdb>=1.0", "polars>=1.0"]` — guarded imports, core
  package imports without it (pattern: `train.py:_require_ml`).
- The lens is read-only by construction: it opens an in-memory DuckDB, attaches nothing
  writable, and only creates views over archive Parquet.
- **Polars convention** (record it, don't build anything): new heavy dataframe work
  (research scripts, future large groupbys/joins) may use Polars; working pandas code is
  never rewritten for it. This lands as a bullet in CLAUDE.md conventions.

## Deliverables

1. **Lens module** `src/ibkr_trader/archive/lens.py`:
   - `connect_lens(settings) -> duckdb.Connection` — in-memory DB; for the S3 backend,
     configure httpfs from the existing `archive_s3_*` settings (endpoint URL for R2,
     region `auto`, keys from Settings — never printed); for the local backend, plain paths.
   - Creates one view per archived dataset (derive names/globs from `parquet_io.py`'s actual
     key layout — read it, don't guess), e.g. `bars` and `raw_payloads`, so users query
     `SELECT ... FROM bars WHERE symbol='XEQT.TO' AND ts >= ...` and pushdown does the rest.
2. **CLI**: `ibkr-trader archive query "SELECT ..."` under the existing `archive_app` —
   runs one statement through the lens, prints a table (and `--csv` for piping). Reject
   obvious non-SELECT statements with a clear message (defense in depth; the lens has
   nothing writable anyway).
3. **Docs**: extend `docs/remote-archive.md` with a "Exploring the archive with DuckDB"
   section: example queries, the read-only rule, and the restore-first rule for anything
   that feeds models/backtests.
4. **CLAUDE.md**: add the Polars convention bullet + one line stating the lens boundary
   (research-only, never imported by signals/backtest/execution).

## Testing (mandatory, same commit)

- `tests/test_archive_lens.py` with `pytest.importorskip("duckdb")`:
  - Local backend: write small Parquet fixtures via the existing `parquet_io` writers into a
    tmp dir, open the lens, assert queries return the right rows and predicate filters work.
  - View names/columns match the documented layout.
  - Non-SELECT via the CLI is rejected; CLI happy path via `CliRunner` on the tmp archive.
- S3/httpfs config path: unit-test that settings map to the expected DuckDB `SET` calls /
  secrets without a network round-trip (no live bucket in tests).
- Check CI installs `[research]` (or the tests silently vanish — testing.md rule).

## Out of scope

- Any write path to the archive from DuckDB; any DuckDB persistence file.
- Feature engineering on top of the lens (that's research work, not this plan).
- Polars adoption work beyond the convention bullet.

## Done when

- [ ] `uv sync --extra research` installs duckdb+polars; core imports without them
- [ ] `archive query` answers a real question against the owner's R2 archive (predicate
      pushdown confirmed by timing or EXPLAIN)
- [ ] remote-archive.md + CLAUDE.md updated
- [ ] Full gate green: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src`
