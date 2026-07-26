# Plan TOOLS-05 — alphalens factor report for ml_lt signals (IC decay, quantiles, turnover)

Read first: [README.md](../README.md) · [CLAUDE.md](../../../CLAUDE.md) ·
[backtest/oos.py](../../../src/ibkr_trader/backtest/oos.py)
(fold predictions — the honest signal source) ·
[signals/dataset.py](../../../src/ibkr_trader/signals/dataset.py)
(how labels/returns are computed) · [db/models.py](../../../src/ibkr_trader/db/models.py)
(`Prediction`, `PriceBar`) ·
[docs/plans/ml-05-oos-backtest.md](../ml-05-oos-backtest.md)
(context: what OOS evidence exists)

## Context

The backtest answers "did the strategy make money after costs?" but not "is the signal
real?" — IC decay over horizons, monotonic quantile returns, turnover, and concentration
are the questions that decide whether ml_lt/ml_lt_ridge predictions carry information or
one lucky tail. alphalens-reloaded computes exactly these from two frames: a factor series
(MultiIndex date×asset → value) and a wide prices frame. The current promotion state
(ridge is the OOS candidate; forward shadow accrues) makes this the right diagnostic to
attach to the evidence pile.

**Scope guard:** this is research reporting. It changes no pipeline behavior, and the
custom engine remains the promotion decision-maker; this report informs, never decides.

## Decisions already made

- Package: `alphalens-reloaded` in the `[research]` extra (create the extra if TOOLS-03
  hasn't landed; guarded imports either way).
- **Signal source: OOS fold predictions**, not in-sample fitted values — feeding alphalens
  in-sample predictions would manufacture a beautiful lie. If per-fold OOS predictions
  aren't currently persisted anywhere reusable, persisting/serializing them is *in scope*
  for this plan (extend the oos run output, don't recompute models here).
- Returns for alphalens: native-currency close-to-close from `price_bars` is acceptable for
  factor diagnostics (the CAD/total-return rigor lives in the real engine — document the
  difference in the report header rather than rebuilding it).
- Periods: monthly-sampled factor; forward periods approximating {1m, 3m, 6m, 12m} in
  trading days. Quantiles: 5 (universe is ~180 names; 10 would be noise).

## Deliverables

1. `src/ibkr_trader/backtest/factor_report.py`:
   - Builders that pull OOS predictions + prices from Postgres into alphalens's expected
     shapes (`get_clean_factor_and_forward_returns` with explicit `periods`, `quantiles`,
     `max_loss` tuned so dropped rows are reported, not silent).
   - A report function returning a plain dict/dataframe summary: mean IC per period, IC
     decay, quantile mean returns (is Q5−Q1 positive and monotonic-ish?), turnover per
     period — plus optional tear-sheet PNG/HTML dump to a local reports dir (gitignored).
2. CLI: `ibkr-trader backtest factor-report [--run-id ...]` printing the summary table.
3. Docs: a short "reading the factor report" section (what IC level is interesting at this
   universe size; what non-monotonic quantiles mean) wherever ML-05/06 notes live.

## Testing (mandatory, same commit)

- `pytest.importorskip("alphalens")` (import name differs from package name — verify).
- Frame builders against in-memory SQLite with synthetic predictions/bars: shapes, index
  types, UTC handling, and that only OOS predictions are selected.
- A rigged perfect-foresight synthetic factor yields strongly positive IC and monotonic
  quantiles; a shuffled factor yields ~0 IC — sanity-checks the whole plumbing.
- CLI smoke via `CliRunner`. CI must install the extra or tests vanish (testing.md rule).

## Out of scope

- Sector exposure analysis (no reliable sector data until ML-01's metadata is populated —
  note it as follow-up if missing).
- Any change to promotion rules, engine, or predictors.

## Done when

- [ ] Factor report runs against the real OOS predictions and the summary is saved with the
      ML evidence notes (memory: ridge is the candidate — this report should say something
      about *why*, e.g. where its IC lives)
- [ ] Full gate green: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src`
