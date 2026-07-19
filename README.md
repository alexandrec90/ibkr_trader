# ibkr-trader

Cloud-hosted Python service that ingests news, social-media and market data, builds
predictions, backtests them, and trades programmatically through Interactive Brokers (IBKR) —
**paper trading first**.

> Personal project. Not investment advice. Paper-trading only until explicitly changed.

## Status

Work in progress. Structure, config, DB schema, ingestion, feature, training and backtest
pieces are in place; paper-trading execution remains guarded and incomplete.

## Quickstart (dev)

Dependencies are managed with [uv](https://docs.astral.sh/uv/). Install it once
(`curl -LsSf https://astral.sh/uv/install.sh | sh`), then:

```bash
# 1. Python env (creates .venv, installs locked deps + the dev group)
uv sync                    # add --extra ml for the ML training deps

# 2. Config
cp .env.example .env      # fill in keys as you get them

# 3. Database (Postgres on host port 5433 — 5432 was taken on the dev machine)
docker compose up -d db
uv run alembic upgrade head

# 4. Sanity
uv run pytest
uv run ibkr-trader --help
```

`uv run <cmd>` runs inside the project venv without activating it; activate the old
way with `. .venv/bin/activate` if you prefer.

## IBKR paper trading

1. Find or activate the separate paper account in Client Portal under
   `Settings → Account Configuration → Paper Trading Account`, then put its `DU...` ID in
   `IBKR_PAPER_ACCOUNT`. Do not use a funded margin/RRSP/TFSA/FHSA account ID here.
2. Either run IB Gateway natively (paper, port 4002) or `docker compose up -d ib-gateway`
   (port 4004, needs `TWS_USERID_PAPER`/`TWS_PASSWORD_PAPER` in `.env`).
3. `ibkr-trader ibkr-check` — connects and prints account + a delayed quote.

## ML training

Install the ML extra with `uv sync --extra ml`, then run `ibkr-trader train run --end
YYYY-MM-DD`. LightGBM capacity selection defaults to deterministic Optuna TPE search with
median pruning (`--search optuna --n-trials 50`). Use `--search grid` to run the original
exhaustive capacity grid. Each artifact's `metadata.json` records the strategy, search space,
trial outcomes, winning fold IC, and elapsed search time; `ibkr-trader train report` displays
the strategy and timing with the existing walk-forward results.

## Layout

```text
src/ibkr_trader/ the service (ingestion / signals / backtest / execution / db)
migrations/      Alembic migrations
docker-compose.yml  postgres + ib-gateway + app
```
