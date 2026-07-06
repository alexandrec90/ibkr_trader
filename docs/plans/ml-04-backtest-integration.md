# Plan ML-04 — Wire the trained model into the backtest leaderboard

Read first: [README.md](README.md) · [CLAUDE.md](../../CLAUDE.md) ·
[signals/predictor.py](../../src/ibkr_trader/signals/predictor.py) (registry contract in the
module docstring) · [signals/portfolio.py](../../src/ibkr_trader/signals/portfolio.py)
(`ScoreAllocator`) · [backtest/compare.py](../../src/ibkr_trader/backtest/compare.py) ·
[docs/registered-account-strategy.md](../registered-account-strategy.md)

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
   [registered-account-strategy.md](../registered-account-strategy.md) §The model; tick
   TODO.md.

## Out of scope

Paper-trading wiring (TODO §4 — separate track), retraining automation, new features.

## Acceptance checklist

- [ ] `ibkr-trader backtest run --strategy ml_lt --account tfsa ...` completes on the dev DB
- [ ] `backtest compare` shows `ml_lt` ranked against all three baselines, params pinned
- [ ] Version-mismatch guard test passes; package works without `[ml]` extra
- [ ] Honest promotion verdict written down (beats baselines after costs: yes/no)
- [ ] pytest / ruff / mypy green
