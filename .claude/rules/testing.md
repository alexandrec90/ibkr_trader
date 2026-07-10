# Testing rules

This project is largely AI-written ("vibe coded"), so tests are the primary defense against
regressions. Be aggressive: if code is testable, it gets a test.

## Non-negotiables

1. **New or changed implemented code ships with tests in the same change.** No "I'll add tests
   later." If you implement a module, you create/extend `tests/test_<module>.py` in the same
   commit. This includes converting a `TODO(skeleton)` stub into a real implementation — the
   stub-to-real conversion is not done until it has a test file.
2. **Never weaken a test to make it pass.** If a test fails after your change, either the code
   is wrong (fix the code) or the test encodes an outdated expectation (say so explicitly and
   justify the new expectation before editing the test).
3. **Safety-critical code has priority coverage.** Anything touching
   `Settings.assert_trading_allowed()`, `RiskChecker`, order placement, or author hashing
   (`stable_hash`) must have explicit tests for both the allowed and the refused/violating
   paths. A change to these files without a corresponding test change is a red flag — stop and
   add the test.
4. **Run the full gate before declaring work done:**
   `pytest && ruff check src tests && ruff format --check src tests && mypy src`.
   Report failures verbatim; never claim green without running it.
5. **Coverage is a ratchet.** CI runs `pytest --cov=ibkr_trader` against the `fail_under`
   floor in `pyproject.toml` (`[tool.coverage.report]`). When total coverage grows, raise the
   floor to just below the new number; never lower it to make a change pass. Check locally
   with `pytest --cov=ibkr_trader` when your change adds meaningful amounts of code.

## How to test each layer

- **Connectors (`ingestion/`)**: never hit the network in tests. Mock the HTTP/provider client
  and assert (a) correct parsing into rows, (b) idempotent upsert on (source, external_id),
  (c) pacing/throttle behavior where it exists (see `tests/test_yahoo_connector.py` for the
  monkeypatched-clock pattern).
- **Signals / backtest**: pure DB-in, DB-out — test with in-memory SQLite and synthetic frames.
  Backtest tests must include at least one no-look-ahead assertion when touching the engine.
- **Execution**: `IbkrBroker` against a fake `ib_async` object, never a live gateway. Every
  order path must show `RiskChecker.check()` is called before submission.
- **CLI (`cli.py`)**: test via `typer.testing.CliRunner` — commands are real logic, not glue.
  At minimum cover argument parsing/validation and the pure helper functions
  (`_read_universe`, output formatters).
- **ML tests** use `pytest.importorskip` for `[ml]` extras — that's fine locally, but remember
  CI must install `.[dev,ml]` or those tests silently vanish. When adding an extras-gated test,
  verify CI actually runs it.

## When finishing any task

Before saying a task is complete, ask: "which behavior did I add or change, and which test
would fail if someone reverted my change?" If no test would fail, the task isn't done.
