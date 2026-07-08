# ibkr-trader

Cloud-hosted Python service that ingests news, social-media and market data, builds
predictions, backtests them, and trades programmatically through Interactive Brokers (IBKR) —
**paper trading first**.

> Personal project. Not investment advice. Paper-trading only until explicitly changed.

## Status

Work in progress. Structure, config, DB schema, ingestion, feature, training and backtest
pieces are in place; paper-trading execution remains guarded and incomplete.

## Quickstart (dev)

```bash
# 1. Python env
python -m venv .venv && . .venv/Scripts/activate   # Windows Git Bash
pip install -e .[dev]

# 2. Config
cp .env.example .env      # fill in keys as you get them

# 3. Database (Postgres on host port 5433 — 5432 was taken on the dev machine)
docker compose up -d db
alembic upgrade head

# 4. Sanity
pytest
ibkr-trader --help
```

## IBKR paper trading

1. Request a paper account in IBKR Client Portal.
2. Either run IB Gateway natively (paper, port 4002) or `docker compose up -d ib-gateway`
   (port 4004, needs `TWS_USERID`/`TWS_PASSWORD` in `.env`).
3. `ibkr-trader ibkr-check` — connects and prints account + a delayed quote.

## Layout

```text
src/ibkr_trader/ the service (ingestion / signals / backtest / execution / db)
migrations/      Alembic migrations
docker-compose.yml  postgres + ib-gateway + app
```
