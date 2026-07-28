# Plan ML-06 — Ridge as a first-class strategy + LightGBM capacity cut

Read first: [README.md](../README.md) · [CLAUDE.md](../../../CLAUDE.md) ·
[signals/train.py](../../../src/ibkr_trader/signals/train.py) (`RidgeModel`, artifact layout) ·
[signals/predictor.py](../../../src/ibkr_trader/signals/predictor.py)
(`MlLongTerm` — the pattern to copy) ·
[ml-04-backtest-integration.md](ml-04-backtest-integration.md)

## Context

The v1 walk-forward record says the **linear sanity floor beat the tree model out of sample**:
ridge rank IC +0.121 ±0.168 (t ≈ 4.3 over 36 test dates) vs lightgbm +0.035 ±0.112 (t ≈ 1.9).
Yet only LightGBM is deployable — `RidgeModel` is never saved. This plan makes ridge a
resolvable strategy so the leaderboard (and ML-05's OOS harness) can rank it after costs, and
takes one disciplined pass at shrinking LightGBM's capacity (400 trees × 31 leaves is a lot
for a ~10k-row panel).

## Deliverables

1. **Persist the ridge artifact:** `train_on_dataset` also fits the final `RidgeModel` on the
   full window and saves it into the same artifact dir (e.g. `ridge.joblib` via joblib) with
   its numeric-column list recorded in `metadata.json`. Record library versions; on load,
   mismatched scikit-learn major/minor ⇒ fail with a retrain hint (pickles are not portable).
2. **Predictor + allocator:** `MlLtRidge` (`name = "ml_lt_ridge"`) copying `MlLongTerm`'s
   contract exactly — no-arg constructible, resolves `ML_LT_MODEL_DIR`/`latest`, version from
   metadata, feature-set-version mismatch ⇒ refuse to score (0.0) loudly, clear error without
   the `[ml]` extra or artifact — plus a registered 15-name/20%-cap allocator wrapper.
3. **LightGBM regularization pass, walk-forward-scored only:** small grid
   (`num_leaves` {7, 15, 31} × `min_child_samples` {20, 50, 100} × `n_estimators`
   {100, 200, 400}), selected by **mean fold IC** (std as tie-break) — never by backtest P&L
   and never peeking past the folds. Fixed seed. Winner becomes the new
   `DEFAULT_LGBM_PARAMS`; record the grid + scores in the artifact metadata.
4. **Retrain + leaderboard:** `train run` a new artifact version, then `backtest run` for
   `ml_lt_ridge` (and re-run `ml_lt`) on tfsa + rrsp over 2021-08-01 → latest bars, same as
   ML-04. If ML-05 has landed, also run both through `backtest oos` — that is the number that
   counts.
5. **Tests:** ridge artifact round-trip (save → load → predict parity on a fixture frame),
   registry resolution, sklearn-version guard, mismatch guard reuse; grid runner smoke on a
   tiny synthetic panel.
6. **Docs:** add `ml_lt_ridge` to the model list in
   [registered-account-strategy.md](../../registered-account-strategy.md) §The model; tick TODO.

## Out of scope

New features or feature-set version bumps (ML-09), OOS harness itself (ML-05), deep models
(decided against — see plans README).

## Acceptance checklist

- [x] `get_allocator("ml_lt_ridge")` resolves; backtest run completes on the dev DB
- [x] Ridge artifact saved/loaded with version + sklearn guard; tests pass
- [x] Grid selected by fold IC only; choice + scores recorded in metadata
- [x] Leaderboard (and OOS run, if ML-05 landed) recorded in completion notes + TODO.md
- [x] pytest / ruff / mypy green

## Completion notes (2026-07-10)

- Artifact `v3` was trained on 12,672 rows / 72 monthly dates (2019-08-30 through
  2025-07-31). The 27-candidate capacity grid selected `num_leaves=7`,
  `min_child_samples=50`, `n_estimators=100` by mean fold rank IC (+0.0916; fold-mean std
  0.0830). These parameters are now `DEFAULT_LGBM_PARAMS`; all candidate scores are in the
  artifact metadata. Overall per-date rank IC was LightGBM +0.092 ±0.103 and ridge
  +0.121 ±0.168 (36 dates).
- Full-window deployed-artifact runs (2021-08-02 through 2026-07-07) are diagnostic and
  in-sample: TFSA ridge +584.2% / Sharpe 1.50 / max DD -30.3% / 80 trades versus LightGBM
  +460.7% / 1.47 / -29.1% / 167 trades; RRSP ridge +587.5% / 1.50 / -30.2% / 80 trades
  versus LightGBM +465.5% / 1.48 / -29.1% / 166 trades.
- The ML-05 stitched OOS rerun (2022-08-31 through 2025-07-31) is the relevant result.
  Regularized LightGBM now leads: TFSA +245.7% / Sharpe 1.74 / max DD -24.7% / 220 trades;
  ridge +147.1% / 1.48 / -25.9% / 94 trades. RRSP was +247.3% / 1.74 / -24.7% for
  LightGBM and +148.0% / 1.48 / -25.9% for ridge. TFSA baselines: momentum +97.7%, equal
  weight +88.1%, buy-and-hold +63.6%.
- Verdict: the capacity cut materially improves LightGBM in this historical OOS harness,
  while ridge remains the lower-turnover deployable alternative. This is still a single,
  survivorship-biased regime; neither model is authorized for live trading. Continue with
  forward shadow evaluation before any paper-account strategy promotion.

## Reading the factor report

Run `ibkr-trader backtest factor-report --run-id ID --output-dir reports/factors` after a
persisted `backtest oos` invocation. Only the predictions made for each fold's held-out rows
are eligible; older OOS runs created before factor reporting landed have no reusable fold
predictions and must be rerun. The report is supporting research evidence, not a promotion
rule and not an order path.

- Mean cross-sectional rank IC around **0.03–0.05** is already interesting for this roughly
  180-name universe; consistency across monthly dates matters more than one large mean. IC
  should normally weaken at longer forward horizons. A sign flip or an isolated strong
  12-month value calls for regime and concentration checks, not celebration.
- Q5−Q1 should be positive, and mean returns should rise approximately from Q1 through Q5.
  Non-monotonic middle buckets mean the score may identify only a tail, may be too noisy to
  rank the full universe, or may proxy for an uncontrolled exposure. The report's
  `monotonic_spearman` is a compact shape diagnostic, not a significance test.
- Turnover is the share of names newly entering each quantile after 1, 3, 6, or 12 monthly
  factor observations. High Q5 turnover makes a paper edge harder to retain after costs even
  when raw IC is positive; use the custom CAD/total-return backtest for the actual cost verdict.
- Alphalens forward returns here are native-currency close-to-close. They intentionally do not
  reproduce CAD conversion, dividend, eligibility, concentration, or transaction-cost logic
  from the promotion backtest. Sector-neutral analysis remains a follow-up until reliable
  point-in-time sector metadata is available.
