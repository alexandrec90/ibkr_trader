# Plan ML-05 — Per-fold out-of-sample backtest (the honest number)

Read first: [README.md](README.md) · [CLAUDE.md](../../CLAUDE.md) ·
[ml-04-backtest-integration.md](ml-04-backtest-integration.md) (completion notes: leaderboard +
why the verdict was "no") · [signals/validation.py](../../src/ibkr_trader/signals/validation.py)
(fold mechanics) · [signals/train.py](../../src/ibkr_trader/signals/train.py) ·
[backtest/engine.py](../../src/ibkr_trader/backtest/engine.py)

## Context

ML-04 put `ml_lt` v1 on the leaderboard and it "won" (+517% vs momentum_lt +228%, TFSA,
2021-08→2026-07) — but the deployed booster was fit on the **full** labeled window, so that
backtest is in-sample and was explicitly **not** accepted as promotion evidence. The honest
record is the walk-forward fold ICs (lightgbm +0.035 ±0.112). This plan converts those folds
into the number the promotion rule actually wants: an **after-cost P&L over the stitched OOS
test span (2022-08-31 → 2025-07-31)** where every decision comes from a model that never saw
its own evaluation period.

Hard constraints a fresh session must know:

- **Leakage rule:** a decision at date t may only use a model whose training labels are fully
  realized before t — i.e. fold k's test months use fold k's model (train end + 12-month purge
  < test start, enforced by `walk_forward_folds`). Never the deployed `models/ml_lt/latest`.
- **FX floor:** USDCAD bars start 2021-07-08; never load a simulation window before that
  (earlier dates value US names at CAD parity).
- **Warm-up:** features need ~12 months of bars before the first decision. Load bars from
  ~2021-08-01 but make **no allocations before the first OOS test date** — and evaluate every
  comparison strategy on the identical decision dates, or the comparison is unfair.

## Deliverables

1. **Engine affordance (additive only):** an optional `eval_start: date | None` on
   `RegisteredStrategyConfig` — before it, the simulator computes nothing and holds cash
   (no rebalance decisions), and reported metrics/equity start at the first decision date.
   Unset ⇒ behavior byte-identical to today (guard with a test).
2. **Fold-switching allocator:** an unregistered allocator (built explicitly, like
   `ScoreAllocator`) wrapping per-fold trained models with a date→fold mapping; returns cash
   for dates outside every fold's test span. Works for both model families (lightgbm, ridge).
3. **CLI `backtest oos`:** builds the dataset (same args as `train run`), trains one model per
   fold in memory (no artifacts needed; pin seed), runs the simulator once per model family
   over the stitched span with `eval_start` = first test date, persists `backtest_runs` rows
   with params pinning `model_version: "oos-walkforward"`, fold count, universe hash and
   `feature_set_version`.
4. **Baselines on identical footing:** `momentum_lt`, `equal_weight`, `buy_and_hold` runs with
   the same bar window and `eval_start`, persisted alongside.
5. **Tests:** leakage guard (mapping refuses a model whose train window + horizon reaches the
   decision date), `eval_start` no-op default, fold-switching allocator returns cash outside
   test spans, one end-to-end smoke on a synthetic panel.
6. **Write-up:** record `backtest compare` output in this plan's completion notes and update
   the ML-04 verdict line in [TODO.md](../../TODO.md) — promote `ml_lt` (or `ridge`) only if it
   beats `momentum_lt` and `buy_and_hold` after costs **here**; otherwise say so plainly.

## Out of scope

Hyperparameter changes (ML-06), new features, paper-forward wiring (ML-07), retraining
automation.

## Acceptance checklist

- [x] `eval_start` unset ⇒ existing engine tests pass unchanged (no behavior drift)
- [x] `ibkr-trader backtest oos ...` completes on the dev DB for lightgbm + ridge
- [x] All five strategies persisted over the identical OOS decision dates; compare ranks them
- [x] Leakage-guard test passes
- [x] Honest verdict written in completion notes + TODO.md
- [x] pytest / ruff / mypy green

## Completion notes (2026-07-10)

Implemented as specified:

- **Engine (additive only):** `RegisteredStrategyConfig.eval_start` — the simulator trims its
  calendar to dates ≥ `eval_start`, so no decisions and no equity points exist before it while
  features still see every loaded warm-up bar; when set it is pinned into `params`. Unset ⇒
  byte-identical (guarded by `test_eval_start_default_is_none_and_leaves_the_run_unchanged`,
  plus a property test that an `eval_start` run equals a run whose calendar simply starts
  there). Also added: `Allocator.asof(day)` (no-op default) so date-aware allocators learn the
  decision date, and `BacktestEngine.run_allocator(...)` for explicitly built allocators with
  `extra_params` merged into the persisted run params.
- **[backtest/oos.py](../../src/ibkr_trader/backtest/oos.py):** `FoldSwitchingAllocator`
  (unregistered; a decision at t uses the latest fold with `test_start ≤ t` — between two
  folds' test blocks that is deliberately the *older* model — and returns cash before the
  first test date / after the last test span). Leakage guard at construction **and** per
  decision: `train_end` + 12-month label horizon must be strictly before the decision date.
  `fit_fold_models` trains one fresh in-memory model per fold (no artifacts, seed pinned);
  `run_oos_backtest` builds the dataset with `train run` semantics, runs both families and
  the three baselines through the same engine over the identical bar window
  ([`--sim-start`, last test date]) and `eval_start`, persisting `model_version:
  "oos-walkforward"`, fold count, universe hash, `feature_set_version`.
- **CLI `backtest oos`:** dataset args mirror `train run` (start 2015-01-01, seed 42,
  test-size 6, min-train 24); `--sim-start` defaults to 2021-08-01 (USDCAD floor).
- **Tests:** [tests/test_oos.py](../../tests/test_oos.py) (leakage guard, cash outside test
  spans, per-fold model routing incl. the boundary day between folds, e2e smoke on a synthetic
  sqlite panel) + `eval_start` tests in [tests/test_engine.py](../../tests/test_engine.py).
  pytest (113) / ruff / mypy green.

### Results (dev DB, TFSA, $100 000 CAD)

`backtest oos --end 2026-07-01 --account tfsa` — dataset 2015-01-01→2026-07-01 (labeled
2019-08→2025-07, 72 dates, 180 names, universe hash `c9630470185b1843` = the v1 artifact's),
folds identical to v1 metadata (6 folds, test span **2022-08-31 → 2025-07-31**), simulation
bars from 2021-08-01, all five strategies on identical decision dates:

| strategy          | ver             | end value CAD | total ret | CAGR  | sharpe | max DD | trades/yr | costs+FX+tax |
|-------------------|-----------------|---------------|-----------|-------|--------|--------|-----------|--------------|
| `ml_lt_ridge_oos` | oos-walkforward | $247 051      | +147.1%   | 35.7% | 1.48   | −25.9% | 31.7      | $3 061       |
| `ml_lt_oos`       | oos-walkforward | $240 068      | +140.1%   | 34.4% | 1.46   | −18.9% | 106.6     | $7 800       |
| `momentum_lt`     | 1               | $197 701      | +97.7%    | 25.9% | 1.46   | −12.9% | 96.8      | $6 253       |
| `equal_weight`    | 2               | $188 140      | +88.1%    | 23.8% | 1.68   | −15.3% | 13.2      | $833         |
| `buy_and_hold`    | 1               | $163 559      | +63.6%    | 18.1% | 1.36   | −15.1% | 0.3       | $193         |

`backtest compare --sort-by sharpe` over the five identical-footing runs (747 days) ranks:
`equal_weight` 1.68 > `ml_lt_ridge_oos` 1.48 > `ml_lt_oos` 1.46 > `momentum_lt` 1.46 >
`buy_and_hold` 1.36. (The ML-04 in-sample `ml_lt` v1 rows still sit above on the full table —
different window, in-sample; ignore them for promotion.)

### Promotion verdict: **qualified yes — on returns; ridge is the candidate, not LightGBM**

The rule as written is met: both model families beat `momentum_lt` and `buy_and_hold` after
all costs over the stitched OOS span, with every decision made by a model that never saw its
own test months. Plainly stated, with the caveats that matter:

- **Ridge is the honest winner.** Highest after-cost return (+147%) at a third of LightGBM's
  turnover (31.7 vs 106.6 trades/yr), and it is *self-consistent* with the fold ICs (ridge
  +0.121 vs lightgbm +0.035). LightGBM's +140% from a ~0.03 IC still smells like luck riding
  a hot tape; ridge's edge has a mechanism behind it. This confirms ML-06's premise: deploy
  `ml_lt_ridge`, cut LightGBM's capacity.
- **The edge is return, not risk-adjusted return.** Sharpe is a statistical tie with
  `momentum_lt` (1.48/1.46 vs 1.46) and *below* `equal_weight` (1.68); ridge also takes the
  deepest drawdown of all five (−25.9%). The models earn more by holding more concentrated
  risk, not by being smarter per unit of risk.
- **The universe is still survivorship-biased** (curated 180 names; `equal_weight` compounding
  at 23.8% shows how hot the pond was), and this is one ~3-year window covering a single
  regime (2022 bottom → 2025 bull). 36 monthly test dates is thin evidence.

Decision: do **not** wire paper trading off this alone. Proceed to **ML-06** (`ml_lt_ridge`
deployable) and start **ML-07** (forward shadow) immediately so forward evidence accrues;
`momentum_lt` stays the reference strategy until the shadow record corroborates the OOS win.
