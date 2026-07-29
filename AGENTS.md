# CLAUDE.md

Personal algo-trading service: ingest news/social/market data → features → predictions →
backtest → trade via IBKR **paper account**. Owner is in Québec, Canada.

Naming: the repo folder is `ibkr_trader`; the Python package is `ibkr_trader`.

## Hard rules

1. **Never enable or suggest enabling live trading.** `ENVIRONMENT=paper` is the ceiling until
   the owner explicitly says otherwise. The gate is `Settings.assert_trading_allowed()` in
   [src/ibkr_trader/config.py](src/ibkr_trader/config.py) — never weaken or bypass it, and keep
   `RiskChecker.check()` in every order path.
2. **Secrets stay in `.env`** (gitignored). Never hardcode keys, never print them, never commit
   `.env`. New config goes through `Settings` + `.env.example`.
3. **Don't invent IBKR API facts.** If the answer is not already encoded in the code/tests,
   research official IBKR sources before changing IBKR behavior.
4. Legal context (Québec): be careful before touching execution, data-retention, or anything
   social-media-privacy adjacent. Social authors are stored **hashed only**.
5. IBKR pacing limits are real: any new historical-data code must throttle
   according to current official IBKR pacing rules.

## Commands

Dependencies are managed with **uv** (lockfile: `uv.lock`, Python pinned in `.python-version`).

```bash
uv sync                        # setup: create .venv, install locked deps + dev group
uv sync --extra ml             # + ML training extras (lightgbm/scikit-learn)
uv add <pkg>                   # add a runtime dep (updates pyproject.toml + uv.lock)
uv lock                        # re-resolve after editing pyproject.toml by hand
docker compose up -d db        # postgres only (usual dev loop)
docker compose --profile ibkr up -d   # + IB Gateway (needs TWS_USERID/PASSWORD in .env)
uv run pytest                  # tests
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src
uv run alembic upgrade head    # apply migrations
uv run alembic revision --autogenerate -m "msg"
uv run ibkr-trader --help      # CLI: ingest / backtest / ibkr-check / serve
```

The initial schema migration exists and is applied to the dev DB (host port **5433**; 5432 is
occupied by another local Postgres). After changing `db/models.py`, autogenerate a new revision
and review it before upgrading.

## Architecture

- `src/ibkr_trader/ingestion/` — one connector per source (news/, social/, market/), all
  implement `base.Connector`, all upsert into Postgres keyed on (source, external_id).
  Connectors own no ambient state: `Settings` and the session factory are **injected**
  (`Connector(settings=..., session_factory=...)`, `self.settings` / `self.session()`), which is
  what keeps the tree extractable — see [docs/plans/active/data-lake.md](docs/plans/active/data-lake.md).
- `src/ibkr_trader/signals/` — features + `Predictor` ABC. Reads/writes DB only.
- `src/ibkr_trader/backtest/` — engine (costs are first-class, no look-ahead) + metrics.
  DB only, no network.
- `src/ibkr_trader/execution/` — `Broker` ABC → `IbkrBroker` (ib_async), `risk.py` pre-trade
  checks.
- `src/ibkr_trader/db/` — SQLAlchemy 2.0 models; migrations via Alembic (`migrations/`).
- Postgres is the single source of truth; models/backtests never call external APIs.

## Conventions

- **Testing is mandatory, not optional** — this codebase is largely AI-written, so every
  implemented (non-stub) module gets a matching `tests/test_<module>.py`, and new/changed code
  ships with tests in the same commit. Full policy: [.claude/rules/testing.md](.claude/rules/testing.md).
- SQLAlchemy 2.0 typed style (`Mapped[...]`), UTC timestamps everywhere.
- Skeleton stubs raise `NotImplementedError` with a `TODO(skeleton)` comment describing the
  intended implementation — replace stub-by-stub, keep the comments' intent.
- Use `ib_async` (maintained fork), never `ib_insync`/raw `ibapi` directly.
- Line length 100 (ruff). Python ≥ 3.11.
