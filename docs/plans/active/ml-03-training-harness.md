# Plan ML-03 — Training harness: dataset, walk-forward validation, LightGBM

Read first: [README.md](../README.md) (label decision is final — do not redesign) ·
[CLAUDE.md](../../../CLAUDE.md) ·
[signals/features.py](../../../src/ibkr_trader/signals/features.py)
(ML-02's builder) ·
[signals/eligibility.py](../../../src/ibkr_trader/signals/eligibility.py) ·
[backtest/engine.py](../../../src/ibkr_trader/backtest/engine.py) (CAD conversion pattern)

## Context

Turn stored bars + features into a supervised dataset and train the first model. Everything
reads Postgres only. The label is fixed by prior decision: **12-month forward total return in
CAD, in excess of XEQT, percentile-ranked cross-sectionally per rebalance date**; rebalance
dates are monthly. Short history is accepted (owner's call — recency over depth): train on
whatever the DB holds, don't gate on fundamentals depth.

## Deliverables

1. **Deps**: add `[project.optional-dependencies] ml = ["lightgbm", "scikit-learn"]` to
   pyproject; guard imports so the core package works without the extra. Add `models/` to
   `.gitignore` (artifacts stay out of git).
2. **Dataset builder** `signals/dataset.py`:
   - `build_dataset(session, universe, start, end) -> pd.DataFrame` — one row per
     (instrument, month-end rebalance date t): features as-of t via ML-02's builder,
     eligibility filter as-of t (reuse the existing screen), label from t→t+12m CAD returns
     (price return incl. dividends where available) minus XEQT's, then per-date percentile
     rank in [0, 1]. Rows lacking a full 12-month forward window are excluded (the dataset
     necessarily ends 12 months before the last bar). USD names convert through the stored
     USDCAD series — reuse the engine's conversion helpers rather than reimplementing.
   - Encode `sector` categorically (LightGBM native categorical or one-hot — implementer's
     choice, record it in artifact metadata).
3. **Walk-forward split** `signals/validation.py`:
   - Expanding-window folds over rebalance dates with a **12-month purge** between each
     train window's end and its test window's start (labels look 12m ahead — without the
     purge, training rows overlap the test period and leak). No shuffled/random splits
     anywhere.
   - Per-fold metric: Spearman rank IC between predicted and realized label per test date,
     reported as mean ± std across dates, plus per-fold summary. A trivial ridge/linear
     model runs alongside LightGBM as a sanity floor.
4. **Training + artifacts** `signals/train.py` + CLI `ibkr-trader train run
   --start ... --end ... [--universe-file tickers.txt]`:
   - Trains on all folds walk-forward, then fits a final model on the full window.
   - Artifact directory `models/ml_lt/<version>/`: model file + `metadata.json` holding
     feature names, `FEATURE_SET_VERSION`, label spec, universe hash, train window, fold
     IC results, library versions, and a monotonically increasing `<version>` (e.g.
     `v1`, `v2`…). `models/ml_lt/latest` marker (file or symlink) points at the newest.
   - `ibkr-trader train report` prints the latest artifact's metadata + ICs.
5. **Tests** (tiny synthetic data, no network, seedable):
   - Purge correctness: no training row's forward window overlaps any test date.
   - Dataset no-look-ahead: label uses only t→t+12m, features only ≤ t.
   - Rank label: per-date values uniform in [0, 1].
   - End-to-end smoke: train on synthetic data, artifact written, report reads it back.
6. Update TODO.md §2 with what landed.

## Honest expectations (record in the run output / report)

Mean OOS rank IC of 0.03–0.05 is good in this domain; ~0 is the common honest outcome, and
the decision metric remains the after-cost backtest (ML-04), not IC.

## Out of scope

Backtest integration (ML-04), hyperparameter search beyond LightGBM defaults + light tuning,
deep models.

## Acceptance checklist

- [ ] `pip install -e .[dev,ml]` clean; package imports without `[ml]` installed
- [ ] `train run` on the ingested 180-name universe completes and writes a versioned artifact
- [ ] Walk-forward report shows per-fold ICs for LightGBM and the linear floor
- [ ] Purge/no-look-ahead tests pass
- [ ] pytest / ruff / mypy green
