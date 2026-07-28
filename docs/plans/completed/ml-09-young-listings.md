# Plan ML-09 — Young-listings experiment (optional; gated on ML-05/06)

Read first: [README.md](../README.md) · [CLAUDE.md](../../../CLAUDE.md) ·
[signals/eligibility.py](../../../src/ibkr_trader/signals/eligibility.py) (the 252-day floor) ·
[signals/features.py](../../../src/ibkr_trader/signals/features.py) (`FEATURE_SET_VERSION`) ·
[ml-05-oos-backtest.md](ml-05-oos-backtest.md) (the harness this experiment is judged by)

## Context

The eligibility screen requires ≥ 252 trading days of listing history, so recent IPOs (e.g.
Palantir's first year) are invisible to every strategy. That floor is **policy, not a model
limitation** — LightGBM handles missing features natively; a 6-month-old name simply has NaN
for `return_12m` and friends. The owner wants to know what relaxing it costs or earns.
Run this only **after ML-05 (and ideally ML-06) land**: the experiment is judged by the
per-fold OOS harness, and only if the core strategy survived it. The core strategy's
discipline must not change as a side effect.

**Migration order warning:** bumping `FEATURE_SET_VERSION` immediately makes every deployed
artifact refuse to score (the ML-04 mismatch guard sends `ml_lt`/`ml_lt_ridge` to cash) until
retrained. Bump and retrain in the same session, in that order.

## Deliverables

1. **Feature-set v2:** add `history_days` (bar count as-of t — the engine's `Candidate`
   already computes it) to `build_features_asof`; bump `FEATURE_SET_VERSION` to `"2"`,
   leaving every v1 feature's semantics untouched (parity test v1-features-under-v2 ==
   v1). Retrain artifacts immediately (see warning above).
2. **Parametrized floor:** `min_history_days` becomes a per-run choice — CLI flag on
   `backtest run` / `backtest oos` / `train run` flowing into `EligibilityLimits`; default
   stays 252 everywhere. The floor used is pinned into `backtest_runs.params`.
3. **The experiment:** dataset + walk-forward ICs + ML-05 OOS backtest at floor 63 vs 252,
   same seeds, same window. A young name's features are mostly NaN — the point is whether the
   model prices that honestly (`history_days` lets it learn the IPO regime instead of being
   blind to it).
4. **Universe process note:** the real gate on "noteworthy new stocks" is the static
   [tickers.txt](../../../tickers.txt), not the floor — document (in the strategy doc) how/when
   new listings get added to `tickers-yahoo.txt` + the aggregate task, and that private
   companies (e.g. SpaceX) are out of scope entirely.
5. **Verdict write-up:** completion notes + TODO — adopt the lower floor only if the OOS
   after-cost number improves without a drawdown blowup; otherwise record the negative result
   and keep 252.

## Out of scope

Private/pre-IPO assets, intraday behavior around IPO dates, sentiment features, changing the
252-day default for the core strategy unless the experiment wins.

## Acceptance checklist

- [x] v2 features: parity test for v1 semantics; `history_days` present; version bumped +
      artifacts retrained in the same session
- [x] Floor is per-run, pinned into params, default unchanged (existing tests untouched)
- [x] 63-vs-252 comparison run through the ML-05 harness; both persisted
- [x] Clear adopt/reject verdict in completion notes + TODO.md
- [x] pytest / ruff / mypy green

## Completion notes (2026-07-10)

- Feature set v2 adds `history_days`, defined as the count of bars observable at the decision
  date (the same quantity used by `Candidate.history_days`). All v1 calculations are unchanged;
  parity, no-look-ahead, young-history, persisted-version, and engine integration tests cover
  the addition. The final core-policy artifact is `models/ml_lt/v6` (feature set v2, floor 252),
  and `models/ml_lt/latest` points to it.
- `backtest run`, `backtest oos`, and `train run` now accept `--min-history-days` (integer ≥ 1,
  default 252). It flows through `EligibilityLimits`; every simulated/persisted result records
  `min_history_days`, and trained metadata records the dataset floor.
- The universe documentation now makes the actual operational gate explicit: review public
  listings quarterly, update `tickers-yahoo.txt`, ingest prices/fundamentals, and run the
  aggregate task. Private/pre-IPO companies are out of scope.

### Experiment (same seed 42, universe, and windows)

Training used 2015-01-01→2026-07-01; both datasets produced the same 72 monthly dates
(2019-08-30→2025-07-31) and universe hash `c9630470185b1843`. Lowering the floor added only
9 rows (12,681 versus 12,672), which is itself evidence that the current static universe
contains almost no young-listing exposure in this window.

| floor | rows | LightGBM OOS rank IC | ridge OOS rank IC |
|---:|---:|---:|---:|
| 63 | 12,681 | +0.088 ± 0.101 | +0.116 ± 0.173 |
| 252 | 12,672 | +0.091 ± 0.102 | +0.118 ± 0.173 |

The ML-05 stitched OOS backtest used TFSA, $100,000 CAD, simulation bars from 2021-08-01,
and identical decision dates 2022-08-31→2025-07-31. Both five-strategy sets were persisted.

| floor | strategy | after-cost return | CAGR | Sharpe | max DD | trades/yr |
|---:|---|---:|---:|---:|---:|---:|
| 63 | `ml_lt_oos` | +252.2% | 52.9% | 1.60 | −32.4% | 55.0 |
| 252 | `ml_lt_oos` | +261.4% | 54.3% | 1.67 | −30.4% | 57.7 |
| 63 | `ml_lt_ridge_oos` | +107.8% | 28.0% | 1.09 | −31.3% | 33.1 |
| 252 | `ml_lt_ridge_oos` | +106.3% | 27.7% | 1.07 | −31.4% | 33.4 |

The floor-independent baselines were identical in both runs: momentum +97.7% / Sharpe 1.46 /
DD −12.9%, equal weight +88.1% / 1.68 / −15.3%, and buy-and-hold +63.6% / 1.36 / −15.1%.

### Verdict: **reject the 63-day floor; keep 252**

The leading LightGBM strategy lost 9.2 percentage points of after-cost return and its maximum
drawdown worsened by 2.0 points at floor 63. Ridge gained only 1.5 return points with essentially
unchanged drawdown, while its IC also declined. This fails the stated adoption rule (improved
after-cost performance without a drawdown blowup). More fundamentally, nine extra rows cannot
answer the intended recent-IPO question well; the static universe process must add noteworthy
public listings before a future rerun can provide meaningful evidence. The 252-day default and
core strategy discipline remain unchanged.
