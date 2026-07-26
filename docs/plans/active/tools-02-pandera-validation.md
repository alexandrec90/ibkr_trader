# Plan TOOLS-02 — pandera data-quality gates between ingestion and features

Read first: [README.md](../README.md) · [CLAUDE.md](../../../CLAUDE.md) ·
[signals/dataset.py](../../../src/ibkr_trader/signals/dataset.py) ·
[signals/features.py](../../../src/ibkr_trader/signals/features.py) ·
[backtest/engine.py](../../../src/ibkr_trader/backtest/engine.py)
(where price frames are loaded) ·
[db/models.py](../../../src/ibkr_trader/db/models.py) (`PriceBar`, `Feature`)

## Context

The codebase is largely AI-written; a silently-bad frame (negative close, duplicated
timestamps, a future-dated bar from a provider glitch) flows straight from ingestion into
features, training, and backtests. pandera gives declarative DataFrame schemas that fail
loudly at the boundary instead. Cheap insurance, no behavior change on good data.

Naming note: `signals/validation.py` already exists and is **walk-forward model validation**
— do not touch it and do not reuse the name. Put schemas in a new module, suggestion:
`src/ibkr_trader/signals/schemas.py`.

## Decisions already made

- pandera is a **runtime** dependency (`uv add pandera`) — the checks run in the real
  pipeline, not just tests.
- Checks fail **hard** (raise) in training/backtest paths: garbage in a research result is
  worse than a crashed run. Ingestion itself is not gated (providers are messy; upserts are
  idempotent) — the gate sits where frames are **read** for downstream use.

## Deliverables

1. **Schemas** in `signals/schemas.py`:
   - `price_frame_schema` — the canonical OHLCV frame shape used by dataset/engine loads:
     `close > 0`, `high >= low`, `high >= close >= low` (allow provider tolerance if real
     data violates strict inequality — check before over-tightening), `volume >= 0` or NaN,
     ts unique + monotonic per instrument, tz-aware UTC, **no timestamp in the future**.
   - `feature_frame_schema` — the frame produced by the feature builder: expected columns
     for the current `feature_set_version`, no ±inf, label/feature dtypes.
   - Keep schemas `lazy=True` validation so one report lists all violations.
2. **Wiring** (small, surgical):
   - Dataset builder (`signals/dataset.py`) validates the price frame after load and the
     assembled training frame before return.
   - Backtest engine validates the price frame at its load point.
   - A module-level `validate: bool = True` escape hatch parameter is fine for perf-sensitive
     callers, default on.
3. **CLI**: `ibkr-trader check-data [--symbols ... | --universe-file ...]` — loads recent
   bars per instrument from Postgres, runs the schema, prints a violation report and exits
   non-zero on failure. Gives the owner a standalone health check.
4. **Docs**: one paragraph in `docs/architecture.md` — where the gates sit and why
   ingestion-side is not gated.

## Testing (mandatory, same commit)

- `tests/test_schemas.py`: for each rule, one passing frame and one violating frame
  (negative close, dup ts, future ts, non-UTC, inf in features) — assert the violation is
  named in the pandera error.
- Existing dataset/engine tests keep passing untouched (their synthetic frames are clean; if
  one fails, the frame was quietly invalid — fix the fixture, not the schema, and say so).
- `check-data` via `CliRunner` against in-memory SQLite: clean DB exits 0, seeded-bad-row DB
  exits non-zero with the row identified.

## Out of scope

- Great Expectations / dbt-style profiling, cross-table referential checks, alerting.
- Auto-repair of bad rows — report and fail only.

## Done when

- [ ] `uv add pandera` + lock committed
- [ ] Schemas cover price + feature frames; wired into dataset builder and engine loads
- [ ] `check-data` CLI works against the dev DB
- [ ] Full gate green: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src`
