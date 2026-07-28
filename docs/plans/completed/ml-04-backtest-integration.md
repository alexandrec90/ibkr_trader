# Plan ML-04 — Wire the trained model into the backtest leaderboard

Read first: [README.md](../README.md) · [CLAUDE.md](../../../CLAUDE.md) ·
[signals/predictor.py](../../../src/ibkr_trader/signals/predictor.py) (registry contract in the
module docstring) · [signals/portfolio.py](../../../src/ibkr_trader/signals/portfolio.py)
(`ScoreAllocator`) · [backtest/compare.py](../../../src/ibkr_trader/backtest/compare.py) ·
[docs/registered-account-strategy.md](../../registered-account-strategy.md)

## Context

ML-03 produces versioned artifacts under `models/ml_lt/`. This plan makes the model a
first-class strategy: resolvable by name from the CLI, run through the exact same engine,
costs, accounts, and leaderboard as `momentum_lt`. The engine and risk paths are not modified.

## Deliverables

1. **Predictor** in `signals/predictor.py`: `@register class MlLongTerm(Predictor)`,
   `name = "ml_lt"`. Per the registry contract it is no-arg constructible: `__init__` resolves
   `models/ml_lt/latest` (override via `Settings` field + `.env.example` entry, e.g.
   `ML_LT_MODEL_DIR`), loads the artifact lazily, sets `self.version` from the artifact's
   metadata. `predict(features)` returns the model's predicted rank centered to a signed
   score (`predicted - 0.5`), and must **refuse to score** (return 0.0) if the artifact's
   `feature_set_version` differs from the code's `FEATURE_SET_VERSION` — log the mismatch
   loudly. Import of the `[ml]` extra stays inside the class so the registry works without
   lightgbm installed (constructing `ml_lt` without it raises a clear error).
2. **Allocator** in `signals/portfolio.py`: registered thin wrapper
   `class MlLtAllocator(ScoreAllocator)` with `name = "ml_lt"` and a no-arg `__init__`
   calling `super().__init__("ml_lt")` — so `backtest run --strategy ml_lt` resolves like
   any other allocator. Keep `max_names=15`, `max_weight=0.20` (same discipline as
   `momentum_lt`).
3. **Run params**: pin `model_version` (artifact) and `feature_set_version` into
   `backtest_runs.params` for `ml_lt` runs so `backtest compare` ranks like with like
   (follow the existing pinning pattern in cli.py/engine.py).
4. **Leaderboard run** (the actual point): for at least `tfsa` and `rrsp`, run `ml_lt`,
   `momentum_lt`, `equal_weight`, and `buy_and_hold` over the ingested window and record
   `backtest compare` output in the plan-completion notes / TODO.md. **Promotion rule:**
   `ml_lt` is only "worth trading" if it beats `momentum_lt` and `buy_and_hold` after costs
   over the walk-forward-validated window — say plainly in the write-up whether it does.
5. **Tests**: registry resolution (`get_allocator("ml_lt")`), predictor with a tiny fixture
   artifact (no lightgbm training in-test if avoidable — a stub model file is fine),
   feature-set-version mismatch → zero scores + warning, graceful error without `[ml]`
   installed.
6. **Docs**: add `ml_lt` to the model list in
   [registered-account-strategy.md](../../registered-account-strategy.md) §The model; tick
   TODO.md.

## Out of scope

Paper-trading wiring (TODO §4 — separate track), retraining automation, new features.

## Acceptance checklist

- [x] `ibkr-trader backtest run --strategy ml_lt --account tfsa ...` completes on the dev DB
- [x] `backtest compare` shows `ml_lt` ranked against all three baselines, params pinned
- [x] Version-mismatch guard test passes; package works without `[ml]` extra
- [x] Honest promotion verdict written down (beats baselines after costs: yes/no)
- [x] pytest / ruff / mypy green

## Completion notes (2026-07-09)

Implemented as specified: `MlLongTerm` predictor (`ml_lt`) + registered `MlLtAllocator`
(top-15 / 20% cap), `ML_LT_MODEL_DIR` setting, `feature_set_version` now pinned into every
run's params alongside `model_version` (for `ml_lt` = the artifact version, e.g. `v1`).
Tests: `tests/test_ml_predictor.py` (registry resolution, stub-artifact loading, rank
centering, mismatch guard, missing-`[ml]` error, params pin). One extra fix found while
running the leaderboard: `equal_weight` never traded — its 1/20 = 5% target weight exactly
equalled the 5% rebalance band (the engine only trades drifts *past* the band). It now holds
a 15-name book like the model strategies (version bumped to 2).

### Leaderboard

Window **2021-08-02 → 2026-07-07** (start bounded by USDCAD coverage, which begins
2021-07-08 — earlier windows would value US names at CAD parity; the window contains the
walk-forward OOS test span 2022-08 → 2025-07). Universe `tickers.txt` (180 names),
$100 000 CAD start, defaults otherwise. TFSA and RRSP runs are near-identical (RRSP drops
US withholding); TFSA numbers shown, all 8 runs persisted to `backtest_runs`.

`backtest compare --sort-by sharpe`:

| strategy       | ver | end value CAD | total ret | CAGR  | sharpe | max DD | trades/yr | costs+FX+tax |
|----------------|-----|---------------|-----------|-------|--------|--------|-----------|--------------|
| `ml_lt`        | v1  | $617 268      | +517%     | 43.7% | 1.61   | −27.5% | 65.4      | $16 002      |
| `momentum_lt`  | 1   | $327 970      | +228%     | 26.7% | 1.45   | −15.7% | 71.2      | $8 780       |
| `equal_weight` | 2   | $222 137      | +122%     | 17.2% | 1.38   | −16.5% | 12.4      | $1 522       |
| `buy_and_hold` | 1   | $198 361      | +98%      | 14.6% | 1.22   | −15.1% | 6.2       | $973         |

### Promotion verdict: **no — not worth trading yet**

Numerically `ml_lt` beats `momentum_lt` and `buy_and_hold` after all costs, on both
accounts, over the window containing the validated span. But that is **not honest
out-of-sample evidence**, so the promotion rule is not satisfied in spirit:

- The deployed v1 booster was fit on **all** labeled rows (rebalances 2019-08 → 2025-07,
  whose 12-month-forward labels consume returns through 2026-07). The entire backtest
  window overlaps its training data — this run is essentially in-sample for the artifact.
- The honest OOS record is the walk-forward fold ICs: lightgbm **+0.035 ±0.112** (and the
  ridge floor beat it at +0.121). A ~0.04-IC signal cannot legitimately produce a 44% CAGR;
  the gap between the in-sample backtest and the OOS IC is what overfitting looks like.
- The curated universe is survivorship-biased (flatters every strategy, selection-heavy
  ones most), and the first ~12 months of the window are feature warm-up.

Before any paper-trading wiring (TODO §4), get an honest after-cost number: backtest with
**per-fold models** (each fold's model trained only on data before its test window) over the
stitched OOS span, and/or evaluate `ml_lt` forward on paper. Until then `momentum_lt`
remains the reference strategy.
