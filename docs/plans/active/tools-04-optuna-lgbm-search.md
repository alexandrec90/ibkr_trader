# Plan TOOLS-04 — Optuna hyperparameter search for LightGBM (replaces the capacity grid)

Read first: [README.md](../README.md) · [CLAUDE.md](../../../CLAUDE.md) ·
[signals/train.py](../../../src/ibkr_trader/signals/train.py) (`LGBM_CAPACITY_GRID` ~line 73,
`select_lgbm_params` ~line 267, `train_on_dataset`, metadata payloads) ·
[tests/test_train.py](../../../tests/test_train.py) ·
[cli.py](../../../src/ibkr_trader/cli.py) (`train_run`)

## Context

`select_lgbm_params` exhaustively evaluates `LGBM_CAPACITY_GRID` — the cartesian product of
`num_leaves × min_child_samples × n_estimators` — across every walk-forward fold, selecting
by fold IC via `selection_key`. That's fine at 27 combos but scales multiplicatively the
moment anyone widens the space (learning rate, regularization, feature fraction). Optuna's
TPE sampler with pruning explores a wider space in fewer fits and prunes bad candidates
mid-fold-loop.

Read `selection_key` and the existing `grid_search` metadata payload carefully before
writing code — the selection semantics (IC objective + tie-break toward lower capacity) and
the artifact metadata trail are behavior to preserve, not incidental detail.

## Decisions already made

- Optuna goes in the `[ml]` extra (`optuna>=3.6`), guarded-import like the rest of train.py.
- **Determinism is required**: `TPESampler(seed=...)` with the run's seed; same dataset +
  seed + n_trials ⇒ same selected params. No parallel workers (they break reproducibility).
- The search space starts as the **same three capacity params with the same bounds** as the
  grid (as ranges, not fixed choices) — proving the machinery before widening the space.
  Widening (learning_rate, lambda_l1/l2, feature_fraction) is a follow-up flag, not default.
- The objective is the existing fold-IC selection criterion; pruning uses per-fold
  intermediate reports (`trial.report` + `MedianPruner`) so a candidate that's clearly bad
  after 2 folds doesn't fit the rest.
- Keep a `--search {optuna,grid}` escape hatch on the train CLI; grid remains as the
  fallback and as the oracle for the equivalence test below. Default flips to optuna.

## Deliverables

1. `select_lgbm_params_optuna(df, folds, *, seed, n_trials=50)` in `train.py` alongside the
   grid version, returning the same `(params, search_metadata)` shape. Metadata records:
   sampler, seed, n_trials, pruned/completed counts, best trial params + IC, search space.
2. `train_on_dataset` takes a `search` argument and threads it through; artifact
   `metadata.json` shows which search produced the params (compare/train_report keep working).
3. CLI: `train run --search optuna --n-trials 50` (defaults), `--search grid` fallback.
4. Docs: short note in the training section of whichever doc describes ML-03 outputs.

## Testing (mandatory, same commit)

- `pytest.importorskip("optuna")` for the new tests; verify CI installs `.[dev,ml]` with
  optuna present or the tests vanish (testing.md rule).
- Determinism: two runs, same seed/synthetic dataset/n_trials ⇒ identical selected params.
- Bounds: every trial's params fall inside the declared space.
- Pruning: with a rigged dataset where one param region is clearly bad, assert some trials
  are pruned (structure the test on trial states, not exact counts).
- Equivalence sanity: on a small synthetic dataset with the space restricted to the grid's
  exact choices and enough trials, optuna's winner is one of the grid's top candidates by IC
  (not necessarily identical — tie-breaks differ; assert IC within tolerance of grid best).
- Metadata payload asserted in the artifact test the way `grid_search` is today.

## Out of scope

- Widened search space by default; multi-objective; optuna storage/dashboard (in-memory
  study only); touching the ridge path.

## Done when

- [ ] `--search optuna` end-to-end on the real dataset produces an artifact whose metadata
      records the study; wall-clock is reported vs the grid
- [ ] Grid path still green (it's the fallback and the oracle)
- [ ] Full gate green: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src`
