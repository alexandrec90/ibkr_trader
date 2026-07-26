# TOOLS-07 — Deferred tools bench (decision record, not an implementation plan)

Tools evaluated 2026-07 and initially deferred. This file exists so future
sessions don't relitigate them from scratch — each entry has an adoption trigger; when a
trigger fires, promote the entry into a real `tools-NN` plan following the pattern of the
others in this directory.

## MLflow — experiment tracking UI

**Promoted 2026-07-19:** the second-model-family trigger fired when Ridge became a
first-class trained artifact. Implemented by [TOOLS-08](tools-08-mlflow-tracking.md); the
historical rationale and gate remain below for the decision record.

**Deferred because:** the training harness already writes versioned artifacts plus
`metadata.json` (params, fold ICs, library versions, universe hash — see
[signals/train.py](../../../src/ibkr_trader/signals/train.py)), and `train report` /
`backtest compare` cover the read side. A tracking UI would duplicate that for one user.

**Adopt when:** experiment volume makes file-diffing metadata painful — e.g. Optuna
(TOOLS-04) searches with widened spaces produce dozens of runs to compare, or a second
model family joins. Then: local file backend only, no server, log the same metadata.json
content — never a parallel source of truth.

## vectorbt — vectorized backtesting

**Deferred because:** the custom engine encodes the things that decide real outcomes here
(CAD conversion, trade budgets, withholding tax, no-look-ahead discipline, cost model) and
is the promotion decision-maker by rule. vectorbt can't express most of that, so today it
would only add a second, less-honest number.

**Adopt when:** there's a genuine *coarse screening* workload — e.g. sweeping hundreds of
signal variants where a cheap directional filter earns its keep before the real engine runs
the survivors. Scope it read-only research (like the DuckDB lens, TOOLS-03): `[research]`
extra, never imported by the decision path, results labeled "screening only".

## skfolio — portfolio construction / optimization

**Deferred because:** allocation today
([signals/portfolio.py](../../../src/ibkr_trader/signals/portfolio.py))
is deliberately simple, and the current bottleneck is signal quality (see ML-05/06/07
evidence chain), not weight optimization. Optimizing weights on a signal that hasn't earned
promotion is polishing noise.

**Adopt when:** a predictor survives OOS + forward shadow and is promoted to paper — then
risk-aware weighting (covariance shrinkage, constraints on concentration/turnover) has a
real signal to allocate. Scope: sits in `signals/portfolio.py` behind the existing
allocator interface, backtested by the custom engine like any other change; `[ml]` or
`[research]` extra.

## Polars — fast dataframes

Not deferred — adopted as a **convention, not a project**, in TOOLS-03: new heavy dataframe
work may use it; working pandas is never rewritten for it. Listed here only for completeness.
