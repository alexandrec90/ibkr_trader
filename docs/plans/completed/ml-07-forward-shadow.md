# Plan ML-07 — Forward shadow evaluation (paper-less forward test)

Read first: [README.md](../README.md) · [CLAUDE.md](../../../CLAUDE.md) ·
[backtest/engine.py](../../../src/ibkr_trader/backtest/engine.py)
(candidate/feature/allocator path to reuse) ·
[db/models.py](../../../src/ibkr_trader/db/models.py) ·
[backtest/costs.py](../../../src/ibkr_trader/backtest/costs.py)

## Context

The only test nothing can leak into is a forward one, and it does not need a broker: record
each strategy's target weights **as-of now**, then score them later against realized bars.
Every month that passes accrues genuinely out-of-sample evidence for the promotion decision —
so this plan is worth landing early and letting the clock run while ML-05/06 proceed.
No orders, no IBKR, no network in the evaluation path — Postgres only.

**Integrity rule (the whole point):** snapshots are only ever taken for the current date.
Never backfill a snapshot for a past date — that would be a backtest wearing a forward-test's
clothes. Enforce it in code (refuse `--asof` in the past beyond a small staleness allowance).

## Deliverables

1. **Table `strategy_snapshots`** (+ Alembic migration, reviewed then `upgrade head`):
   `id` (SqliteFriendlyBigInt PK), `strategy`, `model_version`, `feature_set_version`,
   `ts` (decision date, UTC), `weights` JSON (`{instrument_id: weight}`), `params` JSON,
   `created_at`. Unique on `(strategy, ts)`; re-running the same day upserts.
2. **CLI `snapshot run [--strategy X | --all]`:** for each strategy (default: every registered
   allocator that resolves — `ml_lt`, `ml_lt_ridge` if ML-06 landed, `momentum_lt`,
   `equal_weight`, `buy_and_hold`), load bars up to the latest ingested date, build
   candidates/features as-of that date through the engine's existing path, call
   `allocate`, persist the snapshot. Warn loudly if the latest bar is stale (> 7 days —
   ingestion should run first).
3. **CLI `snapshot report [--horizon-months N]`:** for each snapshot at least N months old
   (default 1, report 3/6/12 as they mature), compute the realized CAD total return of its
   weights from bars strictly after `ts`, versus XEQT over the same span, minus an
   approximate cost of the turnover between consecutive snapshots (reuse
   `RegisteredAccountCostModel`). Summarize per strategy: mean excess vs XEQT, hit rate,
   n snapshots. Plain-English output like `backtest run`'s headline.
4. **Cadence:** a VS Code task (`snapshot: monthly`) chained after the ingestion tasks;
   document the monthly ritual in TODO.md §3 (ingest → snapshot run). `serve` automation is a
   noted follow-up, not in scope.
5. **Tests:** upsert-per-(strategy, ts) idempotency, no-backfill guard, report uses only bars
   > ts (no-look-ahead property test), stale-bar warning, empty-DB behavior.

## Out of scope

Order placement / IBKR / TODO §4, `serve` scheduling, dashboards. Promotion decisions —
this plan only starts the meter; the verdict needs months of snapshots plus ML-05's number.

## Acceptance checklist

- [x] Migration applied; `snapshot run --all` persists rows on the dev DB
- [x] Backfill attempt is refused with a clear error; test covers it
- [x] `snapshot report` computes realized excess returns using only post-`ts` bars
- [x] Monthly task wired and documented in TODO.md
- [x] pytest / ruff / mypy green

## Completion notes (2026-07-10)

- Added the `strategy_snapshots` table and migration `e5f6a7b8c9d0`; applied it to the dev DB.
- `snapshot run --all` recorded all five registered strategies for 2026-07-10. Runs are
  idempotent per `(strategy, ts)`, warn when the newest daily bar is over seven days old, and
  reject dates older than the one-day operational retry allowance.
- `snapshot report` values each holding in CAD from its first bar strictly after the decision
  timestamp, compares the same interval with XEQT, and subtracts approximate consecutive-
  snapshot turnover costs through `RegisteredAccountCostModel`.
- Added the monthly VS Code ingestion → snapshot task and documented the manual ritual in
  `TODO.md`. `serve` scheduling remains out of scope.
- Full gate: 151 tests passed; Ruff lint/format and mypy passed.
