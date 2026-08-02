---
description: IBKR-specific testing policy for safety gates, layer boundaries, and the full local gate
paths:
  - src/**/*.py
  - tests/**/*.py
  - migrations/**/*.py
  - pyproject.toml
  - .github/workflows/**/*.yml
---

# Rule: IBKR testing

The reusable testing baseline belongs in devkit's `engineering.md`. This rule contains only
the additions and exceptions required by IBKR Trader.

## Local non-negotiables

1. **Safety-critical code has priority coverage.** Anything touching
   `Settings.assert_trading_allowed()`, `RiskChecker`, order placement, or author hashing
   (`stable_hash`) must have explicit tests for both the allowed and the refused/violating
   paths. A change to these files without a corresponding test change is a red flag — stop and
   add the test.
2. **Run the full gate before declaring work done.** This intentionally overrides devkit's
   targeted-local-test default: this repository's suite is self-contained and uses in-memory
   SQLite, so the full local gate is the appropriate completion check:
   `pytest && ruff check src tests && ruff format --check src tests && mypy src`.
   Report failures verbatim; never claim green without running it.
3. **Keep the IBKR coverage floor current.** CI runs `pytest --cov=ibkr_trader` against the
   `fail_under` floor in `pyproject.toml` (`[tool.coverage.report]`). When total coverage grows,
   raise the floor to just below the new number. Check locally with `pytest --cov=ibkr_trader`
   when your change adds meaningful amounts of code.
4. **Implemented skeleton modules have matching test modules.** Converting a
   `TODO(skeleton)` stub into a real implementation includes creating or extending
   `tests/test_<module>.py`.

## How to test each layer

- **Connectors and the archive**: these live in the `data-lake` package now, so their tests do
  too — run `uv run pytest` in `../data-lake`, and ship connector changes with tests **there**.
  Same rules as ever: never hit the network, mock the HTTP/provider client, and assert (a)
  correct parsing into rows, (b) idempotent upsert on (source, external_id), (c) pacing/throttle
  behavior where it exists (see that repo's `tests/test_yahoo_connector.py` for the
  monkeypatched-clock pattern). Credentials inject through the settings object
  (`monkeypatch.setattr(SETTINGS, "finnhub_key", ...)`), never an environment variable.
  A change touching both repos is not done until **both** gates are green.
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
