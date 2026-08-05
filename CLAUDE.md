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
docker compose up -d db        # postgres (see "What each container is for" below)
docker compose --profile ibkr up -d   # + IB Gateway (needs TWS_USERID/PASSWORD in .env)
docker compose stop            # stop this project's containers (keeps data; `start` to resume)
uv run pytest                  # tests
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src
uv run alembic upgrade head    # apply migrations
uv run alembic revision --autogenerate -m "msg"
uv run ibkr-trader --help      # CLI: ingest / backtest / ibkr-check / serve / health
```

The initial schema migration exists and is applied to the dev DB (host port **5433**; 5432 is
occupied by another local Postgres). After changing `db/models.py`, autogenerate a new revision
and review it before upgrading.

### What each container is for

- **The standard gate needs none of them.** `uv run pytest`, `ruff`, and `mypy` all run natively —
  the whole test suite uses in-memory SQLite (no `conftest.py`, no testcontainers; see
  [.claude/rules/testing.md](.claude/rules/testing.md)). Never start Docker to run tests.
- **`db`** — needed for `alembic upgrade head` / `--autogenerate` and any
  `ibkr-trader ingest|backtest|serve` run against the real Postgres dataset.
- **`ib-gateway`** (the `ibkr` profile) — live IBKR API calls (`ibkr-check`, `serve` against the
  gateway).
- **`app`** (the `app` profile) — the `ibkr-trader serve` scheduler. Rebuild with `--build` after
  a code change or the container keeps running the old image.

Prefer `stop` over `down` so the `pgdata` volume and its schema survive.

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
- `src/ibkr_trader/scheduler.py` — the APScheduler wiring behind `serve`. **A split
  candidate, deliberately not split yet** — read this before proposing to move it:
  - Six of its eight jobs are pure data-lake ingestion (`reddit_poll`,
    `finnhub_news_poll`, `newsapi_poll`, `finnhub_backfill`, `trends_poll`, `prices_poll`),
    each importing straight from `data_lake.ingestion.*`. Those are the movable part.
  - Two are **not** and are what block a wholesale move: `sentiment_score` calls
    `ibkr_trader.signals.sentiment.score_pending`, and `prune_raw` calls
    `ibkr_trader.maintenance.prune_scored_raw`. Moving the file as-is would make
    `data_lake` import this package, which is the one thing it must never do —
    `tests/test_lake_seam.py::test_package_never_imports_a_consumer` over there fails on
    it by filename. **The split, not the move, is the work.**
  - The real cost is config, not code: `build_scheduler()` reads twenty-one fields off
    `Settings` (`poll_*`, `newsapi_*`, `finnhub_backfill_*`, `prune_raw_*`,
    `score_sentiment_minutes`, `news_universe_file`, `trends_*`, `fx_pairs`,
    `scheduler_health_file`), and **none of them are in `data_lake.settings.LakeSettings`**,
    which covers provider credentials and archive location only. A scheduler over there
    needs a `SchedulerSettings` Protocol first — see that repo's `CLAUDE.md` for how to
    extend the seam.
  - **A guarded job's failure is invisible unless it is recorded.** `_guard` swallows the
    exception so one dead source cannot stop the rest, and APScheduler then logs the run as
    "executed successfully" — which hid a six-day outage (a stopped database) in July 2026.
    Every run therefore records to `job_health`, which writes `logs/scheduler-health.json`;
    `ibkr-trader health` reads it and exits non-zero on a failing, stale or never-run job.
    **Anything added to `_guard` must keep that artifact written on the failure path too.**
  - **The same trap one level down:** the polls catch per-symbol errors so one dead ticker
    cannot abort a run, which means a *systemic* failure reads as an empty result. A database
    outage made all 190 Finnhub symbols fail their write and logged "upserted 0 articles
    across 190 symbols" — indistinguishable from a quiet news day, and it stayed that way for
    two weeks. `_fail_if_every_item_failed` re-raises when nothing at all succeeded. Any new
    per-item loop needs the same call, or it will hide the next outage the same way.
  - data-lake also carries no scheduler machinery at all today: no apscheduler
    dependency, no `_guard`-equivalent. The `archive` and `research` extras are the
    pattern to copy for adding one.
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
