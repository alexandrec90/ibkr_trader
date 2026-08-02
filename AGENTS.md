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

The `data-lake` package must be checked out **beside** this repo (`../data-lake`) — it is an
editable path dependency, so `uv sync` fails without it:
`git clone https://github.com/alexandrec90/data-lake.git ../data-lake`.

```bash
uv sync                        # setup: create .venv, install locked deps + dev group
uv sync --extra ml             # + ML training extras (lightgbm/scikit-learn)
uv add <pkg>                   # add a runtime dep (updates pyproject.toml + uv.lock)
uv lock                        # re-resolve after editing pyproject.toml by hand
docker compose up -d db        # postgres — only when a command actually needs it (see below)
docker compose --profile ibkr up -d   # + IB Gateway (needs TWS_USERID/PASSWORD in .env)
docker compose stop            # stop this project's containers (keeps data; `start` to resume)
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

### Containers are OFF by default (laptop with limited RAM/CPU)

The owner's dev machine is a memory-constrained laptop running several projects' Docker stacks
at once. **Leave containers stopped unless the specific command you're about to run needs one,
and stop it again when you're done.** Do not start containers "just in case" or leave them
running after a task.

- **The standard gate needs NO container.** `uv run pytest`, `ruff`, and `mypy` all run natively —
  the whole test suite uses in-memory SQLite (no `conftest.py`, no testcontainers; see
  [.claude/rules/testing.md](.claude/rules/testing.md)). Never start Docker to run tests.
- **`db` container** is needed only for: `alembic upgrade head` / `--autogenerate` (validating
  migrations) and any `ibkr-trader ingest|backtest|serve` run against the real Postgres dataset.
- **`ib-gateway`** (the `ibkr` profile) is needed only for live IBKR API calls (`ibkr-check`,
  `serve` against the gateway). It's the heaviest container — a full Java GUI + VNC — so it stays
  down until an IBKR-touching task actually requires it.

On-demand pattern — start the one container you need, run, then put it back down:

```bash
docker compose up -d db && docker compose exec db pg_isready -U trader  # wait for healthy
uv run alembic upgrade head
docker compose stop db                                                  # release RAM when done
```

Use `stop` (not `down`) so the `pgdata` volume and schema survive. If you started a container for
a task, you own stopping it before you finish.

## Architecture

- **Ingestion and the archive are no longer in this repo.** They live in the private
  [`data-lake`](https://github.com/alexandrec90/data-lake) package (checked out as a sibling
  directory, wired in as an editable path dependency), so a second project can reuse them:
  - `data_lake.ingestion` — one connector per source (news/, social/, market/), all implement
    `base.Connector`, all upsert into Postgres keyed on (source, external_id).
  - `data_lake.archive` — cold-data offload to object storage as Parquet (intraday bars,
    scored raw payloads) with verify-before-delete, plus the catalog and the DuckDB lens.
    Needs the `[archive]` extra. See [docs/operations/remote-archive.md](docs/operations/remote-archive.md).

  The package owns no config and no engine: `src/ibkr_trader/lake.py` hands it ours via
  `data_lake.configure(...)`, called from the CLI's root callback and `build_scheduler()`.
  Changing a connector means editing `../data-lake` and committing **there** — the editable
  install means the change is live here immediately, with no reinstall and no lockfile bump.
- `src/ibkr_trader/signals/` — features + `Predictor` ABC. Reads/writes DB only.
- `src/ibkr_trader/backtest/` — engine (costs are first-class, no look-ahead) + metrics.
  DB only, no network.
- `src/ibkr_trader/execution/` — `Broker` ABC → `IbkrBroker` (ib_async), `risk.py` pre-trade
  checks.
- `src/ibkr_trader/db/` — `trading_models.py` (orders, executions, predictions, backtests,
  strategy state — **never** leave this repo) declared against the *package's* `Base`, so one
  `Base.metadata` still covers the whole database. `models.py` re-exports both halves; import
  it, never `data_lake.db.models` directly, or Alembic autogenerate stops seeing the trading
  tables and proposes dropping them. Migrations stay here (`migrations/`).
- Postgres is the single source of truth; models/backtests never call external APIs.

## Conventions

- **IBKR-specific testing policy:** safety-gate coverage, layer-specific test strategy, and this
  repo's full local completion gate live in [.claude/rules/testing.md](.claude/rules/testing.md).
- SQLAlchemy 2.0 typed style (`Mapped[...]`), UTC timestamps everywhere.
- Skeleton stubs raise `NotImplementedError` with a `TODO(skeleton)` comment describing the
  intended implementation — replace stub-by-stub, keep the comments' intent.
- Use `ib_async` (maintained fork), never `ib_insync`/raw `ibapi` directly.
- The DuckDB archive lens is research-only; `signals/`, `backtest/`, and `execution/` never
  import it (enforced in `tests/test_db_models_split.py`). Restore archived data to Postgres
  before it feeds the real pipeline.
- New heavy dataframe work may use Polars; never rewrite working pandas code merely to adopt it.
- Line length 100 (ruff). Python ≥ 3.11.
