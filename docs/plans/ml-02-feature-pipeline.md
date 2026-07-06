# Plan ML-02 — Shared, versioned feature pipeline

Read first: [README.md](README.md) · [CLAUDE.md](../../CLAUDE.md) ·
[signals/features.py](../../src/ibkr_trader/signals/features.py) ·
[backtest/engine.py](../../src/ibkr_trader/backtest/engine.py) (`_features_asof`, `Series`,
benchmark loading) · [tests/test_engine.py](../../tests/test_engine.py)

## Context

Today the only features are `return_12m` and `volatility`, computed inline in the engine's
`_features_asof`. Training (ML-03) and backtesting must use **the same feature code** so they
can never disagree, and features must be versioned so a saved model knows what it was trained
on. This plan builds that shared builder and moves the engine onto it.

Design: a **pure core + thin DB wrapper**. The core takes in-memory inputs (price series,
dividends, share counts, sector, benchmark series) and a date, returns `dict[str, float]` —
no session, no network, trivially testable. The engine calls the core directly with the
`Series` it already loads. A wrapper loads inputs from Postgres and can persist snapshots to
a new `features` table for training-set reuse.

## Deliverables

1. **Feature core** in `signals/features.py`:
   - `FEATURE_SET_VERSION = "1"` module constant.
   - `build_features_asof(inputs, day) -> dict[str, float]` where `inputs` is a small
     dataclass (closes/volumes with dates, dividends ≤ day, share counts ≤ day, sector,
     benchmark closes). **Only data ≤ day may influence the output** — this is the
     no-look-ahead contract; features with insufficient history are simply absent.
   - Feature set v1 (price + ML-01 data):
     - returns: `return_1m`, `return_3m`, `return_6m`, `return_12m`, `momentum_12_1`
       (12m return skipping the most recent month)
     - risk: `volatility` (252d, annualized — keep semantics identical to the current
       engine version), `volatility_60d`, `downside_deviation_252d`, `max_drawdown_252d`
     - price position: `pct_off_52w_high`, `volume_zscore_60d`
     - benchmark-relative: `excess_return_3m`, `excess_return_12m` (vs the benchmark series)
     - corporate: `dividend_yield_ttm` (trailing 12m dividends ÷ close),
       `dividend_growth_3y` (when history allows), `log_market_cap` (close × latest
       share count ≤ day), `sector` handled as a string in the payload — the *numeric*
       dict for allocators omits it; training encodes it categorically.
2. **`features` table** (+ migration): `instrument_id` FK, `ts` (UTC DateTime),
   `feature_set_version` (String), `payload` (JSONB), unique
   `(instrument_id, ts, feature_set_version)`, `SqliteFriendlyBigInt` PK. Wrapper
   `build_daily_features(session, instrument_ids, dates)` computes via the core and upserts.
3. **Engine integration**: `_features_asof` delegates to the core (constructing `inputs`
   from its loaded `Series` + benchmark + ML-01 tables when available). Existing behavior
   must not silently change: `return_12m` and `volatility` values equal today's
   implementation on the same bars (assert in tests). Missing ML-01 data (not yet ingested)
   degrades to price-only features, not an error.
4. **Tests**:
   - No-look-ahead property: compute features at day t, append later bars/dividends,
     recompute at t → identical output.
   - Parity: `return_12m`/`volatility` match the pre-refactor engine values on fixture data
     (guards the `momentum_lt` baseline's results).
   - Upsert idempotency on the `features` table; version key respected.
5. Tick TODO.md §2 items (`build_daily_features`) as done; note follow-ups there.

## Out of scope

Labels, training, sentiment features, `MomentumBaseline.predict()` (leave the stub).

## Acceptance checklist

- [ ] Engine backtest of `momentum_lt` on existing DB data produces the same metrics as
      before the refactor (run once before starting to capture the baseline numbers)
- [ ] `features` migration applied; snapshots persist and re-run idempotently
- [ ] No-look-ahead property test passes
- [ ] pytest / ruff / mypy green
