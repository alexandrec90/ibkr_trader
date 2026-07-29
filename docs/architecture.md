# Architecture

```text
                        ┌─────────────────────────────────────────────┐
                        │                  scheduler                  │
                        │        (APScheduler jobs, cli `serve`)      │
                        └───────┬─────────────┬─────────────┬─────────┘
                                │             │             │
                     ┌──────────▼──┐   ┌──────▼──────┐   ┌──▼──────────────┐
   NewsAPI ─────────►│  ingestion  │   │   signals   │   │    execution    │
   Finnhub ─────────►│ news/social │   │  features + │   │ Broker ABC      │
   AlphaVantage ────►│ /market     │   │ predictions │   │  └ IbkrBroker ──┼──► IB Gateway
   FMP ─────────────►│ connectors  │   └──────┬──────┘   │ risk checks     │    (Docker, paper)
   Reddit (PRAW) ───►│             │          │          └──┬──────────────┘
   pytrends ────────►└──────┬──────┘          │             │
   IBKR hist. ──────────────┤                 │             │
                            ▼                 ▼             ▼
                        ┌─────────────────────────────────────────────┐
                        │              PostgreSQL (SQLAlchemy)        │
                        │ instruments · price_bars · news_articles ·  │
                        │ social_posts · trend_points · predictions · │
                        │ orders · executions · backtest_runs         │
                        └───────────────────────┬─────────────────────┘
                                                │
                                        ┌───────▼────────┐
                                        │    backtest    │
                                        │ engine+metrics │
                                        └────────────────┘
```

## Modules (`src/ibkr_trader/`)

- `config.py` — pydantic-settings; everything from env/.env. Paper-by-default safety gate.
- `db/` — SQLAlchemy 2.0 models + session factory. Alembic migrations in `migrations/`.
- `ingestion/` — one connector per source (`base.Connector` interface). Pure "fetch → upsert".
  Both ambient dependencies are injectable: `Connector(settings=..., session_factory=...)`, each
  falling back lazily to the process-wide one, so the tree owns neither config nor the engine
  (the seam for [the data-lake plan](plans/active/data-lake.md)).
- `signals/` — feature building (sentiment, mention counts, returns) and model interface
  (`Predictor` ABC). Models read features from Postgres, write `predictions` rows.
- `backtest/` — replays stored bars against a strategy; costs/slippage models; metrics
  (Sharpe, max drawdown, CAGR, hit rate). No network access — DB only, so runs are reproducible.
- `execution/` — `Broker` ABC with `IbkrBroker` (ib_async) implementation; `risk.py` pre-trade
  checks. Paper account first, always.
- `cli.py` — Typer CLI: `ingest`, `backtest`, `ibkr-check`, `serve`.

## Key decisions

1. **Postgres is the single source of truth**; models and backtests never hit external APIs.
2. **Same `Broker` interface for paper and live** — flipping is config, not code.
3. **Connectors are dumb and idempotent**; smarts live in `signals/`.
4. **ib_async over raw ibapi** — maintained, pythonic, event-loop friendly.
5. Cloud target: any Docker host; Canadian region preferred (see legal doc). Compose file works
   the same locally and on a VM.

Pandera data-quality gates sit at the PostgreSQL-to-research boundary: daily OHLCV frames are
validated before the dataset builder or backtest engine converts them into in-memory series,
and the assembled, versioned training frame is validated before it is returned. Checks fail
hard because silently invalid research results are worse than a stopped run. Ingestion remains
ungated because provider payloads are messy and connector upserts are idempotent; bad stored
rows can be inspected independently with `ibkr-trader check-data` rather than blocking a whole
provider refresh.

## Suggested build order (future sessions)

1. `ingest prices` end-to-end for a small symbol list (one market connector, e.g. FMP or
   Alpha Vantage) — schema is already migrated.
2. Reddit + NewsAPI connectors live; naive ticker extraction + daily sentiment features.
3. Backtest engine with costs; baseline strategies (buy-and-hold, momentum) for calibration.
4. IB Gateway container up (paper); `ibkr-check` verifies connect + delayed quotes.
5. First paper-traded strategy behind risk caps; reconciliation loop.
