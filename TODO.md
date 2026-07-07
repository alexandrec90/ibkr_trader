# TODO

Status of the build-out (order matches [docs/architecture.md](docs/architecture.md)).
Keep this file updated as items land — check things off, don't delete them.

## ✅ Done

- [x] Project skeleton: config (paper-by-default gate), DB models, Alembic (initial migration
      applied), Docker compose (Postgres 5433 + opt-in ib-gateway), CLI, CLAUDE.md, tasks.json
- [x] IBKR research docs ([docs/ibkr/](docs/ibkr/)) + Québec legal notes + data-source survey
- [x] Multi-asset implementation roadmap ([docs/multi-asset-roadmap.md](docs/multi-asset-roadmap.md))
- [x] **FMP price connector end-to-end** — `ingest prices <SYM> --source fmp`, upserts
      `price_bars`, handles `.TO` symbols, tested (`tests/test_fmp_connector.py`);
      `tickers.txt` universe + VS Code ingest tasks
- [x] **Registered-account long-term strategy — vertical slice** (docs/registered-account-strategy.md):
      account tax profiles ([accounts.py](src/ibkr_trader/accounts.py), RRSP/TFSA/FHSA/LIRA/nonreg
      US-withholding), eligibility screen ([eligibility.py](src/ibkr_trader/signals/eligibility.py)),
      portfolio `Allocator` layer + registry + `ScoreAllocator` adapter
      ([portfolio.py](src/ibkr_trader/signals/portfolio.py)), churn+withholding cost model
      ([costs.py](src/ibkr_trader/backtest/costs.py)), and the **CAD/FX portfolio simulator**
      with per-account trade budget + benchmark ([engine.py](src/ibkr_trader/backtest/engine.py)).
      `backtest run` wired with the plain-English P&L headline. Cost function models commissions,
      slippage, spread, a churn penalty, US-dividend withholding, **and the CAD↔USD conversion
      spread** when the currency mix shifts. Tests cover accounts, eligibility, portfolio, costs,
      and engine (no-look-ahead, budget, FX return, FX conversion cost, withholding). Instrument
      metadata (`asset_class`, `leveraged`) + migration `b1c2d3e4f5a6` — **run `alembic upgrade
      head`** on the dev DB.
- [x] **Yahoo price connector** ([yahoo.py](src/ibkr_trader/ingestion/market/yahoo.py)) —
      `ingest prices XEQT.TO --source yahoo`; covers what FMP's free tier gates (GOOG, TSX/
      `.TO` symbols → HTTP 402). Unofficial scraper: hard ≥2 s throttle between requests,
      adjusted bars by default (`what_to_show=ADJUSTED_LAST`). Tested
      (`tests/test_yahoo_connector.py`). Ingest lists split: `tickers.txt` = backtest
      universe, `tickers-fmp.txt` / `tickers-yahoo.txt` = per-connector ingest lists
      (VS Code tasks updated).
- [x] **FMP FX connector** ([fmp_fx.py](src/ibkr_trader/ingestion/market/fmp_fx.py)) —
      `ingest fx --pair USDCAD`, stores a `CASH` instrument's daily bars (CAD per USD) for the
      simulator's CAD conversion; tested (`tests/test_fmp_fx_connector.py`) + VS Code task.
      **[verify]** FMP EOD coverage for FX pairs against the live plan.

- [x] **Universe widened to ~180 names** (TSX 60-ish + S&P 100-ish + CAD core ETFs) in
      `tickers-yahoo.txt`; `.vscode/aggregate_tickers.py` + task
      `tickers: aggregate universe` regenerates `tickers.txt` (comment-free — cli.py's
      universe reader doesn't skip `#`) from the per-connector lists, warning on
      TSX/US bare-symbol collisions (engine resolves universe lines by symbol only).
      Curated list = survivorship-biased; say so in backtest write-ups.

## 1 · Ingestion (fill the database)

- [ ] **NewsAPI connector** ([newsapi.py](src/ibkr_trader/ingestion/news/newsapi.py)) —
      pagination, upsert on `(source, external_id=hash(url))`, set `fetched_at`
- [ ] **Finnhub company-news connector** ([finnhub_news.py](src/ibkr_trader/ingestion/news/finnhub_news.py))
- [ ] **Reddit connector** ([reddit.py](src/ibkr_trader/ingestion/social/reddit.py)) — PRAW,
      4 subreddits from `Settings.subreddits`, author **hashed only**, re-poll updates
      score/num_comments
- [ ] **Google Trends connector** ([google_trends.py](src/ibkr_trader/ingestion/social/google_trends.py)) —
      pytrends, tiny volume + long backoff, consistent timeframe windows
- [ ] **IBKR historical connector** ([ibkr_historical.py](src/ibkr_trader/ingestion/market/ibkr_historical.py)) —
      ib_async, token-bucket throttle for pacing rules
      ([docs/ibkr/03](docs/ibkr/03-market-data-and-historical.md)); cache `conId` on instruments
- [ ] Alpha Vantage / Finnhub candles — **optional**, only if FMP+Yahoo coverage proves
      insufficient (free tiers are tight; verify Finnhub candle access first)
- [ ] Questrade API connector — **candidate** (owner is a Questrade client; API free for
      clients, OHLC candles capped at 2 000/request, OAuth refresh-token flow). Evaluate as
      an official-API replacement for the Yahoo scraper if Yahoo starts blocking us.
- [x] **Yahoo fundamentals connector** ([yahoo_fundamentals.py](src/ibkr_trader/ingestion/market/yahoo_fundamentals.py)) —
      `ingest fundamentals AAPL` upserts dividends / share counts / income-balance-cashflow
      statements / sector+industry / earnings dates; ETFs (e.g. XEQT.TO) ingest dividends only,
      gracefully. Snapshot-forward: statements re-upserted each run with `fetched_at` refreshed
      while `first_seen` (set on insert) is never touched, and each period's `report_date` is
      inferred from the nearest earnings event within 120 days for point-in-time lagging.
      Shares the module-level Yahoo throttle (refactored into
      [yahoo_common.py](src/ibkr_trader/ingestion/market/yahoo_common.py)). Migration
      `c2d3e4f5a6b7` (Instrument.sector/industry + `dividends`, `share_counts`,
      `fundamental_snapshots`, `earnings_events`) applied to dev DB. Tested
      (`tests/test_yahoo_fundamentals_connector.py`); VS Code task
      `ingest: yahoo fundamentals (tickers-yahoo.txt)` + `--source yahoo-fundamentals` batch path.
      `.info` ratios are current-only → live eligibility/solvency screen, never backtest features.
      Deep US history if ever needed: SEC EDGAR XBRL (free, US-only) or paid FMP.
- [ ] Shared connector plumbing: retry/backoff via tenacity, structured logging, per-source
      rate-limit config
- [ ] `serve` command ([cli.py](src/ibkr_trader/cli.py)) — APScheduler jobs: daily FMP refresh
      of `tickers.txt`, periodic Reddit/news polls
- [ ] Extend `SqliteFriendlyBigInt` PK pattern to the remaining tables as their tests appear
      (now `price_bars` + `backtest_runs`; still to do: `predictions`, `orders`, `executions`, …)

## 2 · Signals & features

- [ ] `score_sentiment()` ([features.py](src/ibkr_trader/signals/features.py)) — start with
      VADER; persist into `news_articles.sentiment` / `social_posts.sentiment`
- [ ] Ticker extraction: stoplist for WSB false positives (CEO/YOLO/DD…), validate against
      `instruments`; backfill `symbols` arrays on stored posts/articles
- [ ] `build_daily_features()` — per symbol/day: returns, volume z-score, mention counts,
      mean sentiment, trends delta
- [x] **Predictor registry** ([predictor.py](src/ibkr_trader/signals/predictor.py)) —
      `@register` / `get_predictor` / `available`; `MomentumBaseline` registered so models
      (long-term vs short-term, ML vs baseline) resolve by name. Tests:
      `tests/test_predictor_registry.py`
- [ ] `MomentumBaseline.predict()` ([predictor.py](src/ibkr_trader/signals/predictor.py)) +
      persist `predictions` rows (with feature snapshot) — body still stubbed (needs features)
- [ ] CLI command to run feature build + prediction for a date range

## 3 · Backtesting

- [x] `BacktestEngine.run()` ([engine.py](src/ibkr_trader/backtest/engine.py)) — portfolio-weights
      path: load bars from DB, decide at close(t) → fill at open(t+1), CAD/FX conversion,
      per-account trade budget, `RegisteredAccountCostModel`, track equity. (Score-signal /
      intraday path still to come.)
- [x] Persist `backtest_runs` (params + metrics) and wire `backtest run` with a plain-English
      P&L headline (account, budget, cost model, model_version pinned into `params`).
- [x] **`backtest compare` CLI + [compare.py](src/ibkr_trader/backtest/compare.py)** — rank
      persisted `backtest_runs` by a metric (the model leaderboard); runs missing the metric
      sort last. Pin `model_version` + `horizon` into run `params` for fair ranking. Tests:
      `tests/test_backtest_compare.py`
- [ ] Validate cost model numbers against IBKR Canada's actual fee schedule (commission,
      spread/slippage bps) — currently conservative placeholders in `backtest/costs.py`.
- [x] Baselines: `buy_and_hold` (XEQT) and `equal_weight` allocators, plus `momentum_lt`, so
      models have an honest benchmark (the engine runs buy-and-hold alongside every strategy).
- [x] Guardrail tests: no look-ahead (fill at open t+1; features/eligibility use data ≤ t),
      trade-budget cap, FX-to-CAD, dividend withholding. **Still TODO:** survivorship-bias note
      in run output (IBKR has no delisted history).
- [x] Multi-provider stores don't double-count: `_load_series` picks exactly one `source` per
      instrument/bucket (widest window coverage wins, `SOURCE_PREFERENCE` breaks ties) — safe
      to mix providers across symbols (e.g. FMP for US names, Yahoo for XEQT). Tested in
      `tests/test_engine.py`.
- [ ] Fundamental solvency screen (market cap, distress/default) once fundamentals are ingested;
      precise per-security dividend cashflows to replace the yield-based withholding drag.

## 4 · IBKR paper trading

**Human prerequisites (can't be automated):**
- [ ] Request paper account in Client Portal; note the `DU…` username → `.env`
      ([docs/ibkr/02](docs/ibkr/02-paper-trading.md))
- [ ] Decide market data: free delayed vs. share live subscriptions with paper (24 h to apply)
- [ ] First `docker compose --profile ibkr up -d` login + 2FA approval; plan for the ~weekly
      Sunday re-auth ([docs/ibkr/05](docs/ibkr/05-running-in-docker.md))

**Code:**
- [ ] `ibkr-check` CLI — connect, print server time, managed accounts, one delayed quote
- [ ] `IbkrBroker` ([ibkr_broker.py](src/ibkr_trader/execution/ibkr_broker.py)) — connect
      (fail loudly if account isn't `DU…` while `ENVIRONMENT=paper`), positions,
      account_summary
- [ ] Order placement: qualify contract → `whatIf` preflight → place → persist `orders` row
      (orderId + permId) — keep `assert_trading_allowed()` + `RiskChecker.check()` in the path
- [ ] Fill/consistency loop: `execDetails`/`commissionReport` → `executions` rows; periodic
      reconcile vs. `reqOpenOrders`/`reqPositions` (orderStatus callbacks aren't guaranteed)
- [ ] Implement the real `RiskChecker` checks ([risk.py](src/ibkr_trader/execution/risk.py)):
      notional via live quote, daily cap from `orders`, position cap, trading-hours, halt flag
- [ ] Paper trading loop in `serve`: predictions → target positions → orders (only after §3
      shows something worth trading)

## 5 · Cloud deployment (after paper loop works locally)

- [ ] Pick host (Canadian region preferred — see [docs/legal-quebec-canada.md](docs/legal-quebec-canada.md));
      provision Docker VM
- [ ] Secrets management (not `.env` on disk), Postgres backups, log shipping
- [ ] Alerting: broker disconnected / 2FA needed / job failures (email or push)
- [ ] CI: GitHub Actions running ruff + mypy + pytest

## Housekeeping

- [ ] Chase the `[verify]` markers in [docs/ibkr/](docs/ibkr/) as each area gets touched
      (gnzsnz env-var names before first gateway run; TSX exchange naming before trading CAD)
- [ ] `mypy src` isn't clean-guaranteed yet — run and fix once implementations start landing
- [ ] Commit cadence: working tree currently has uncommitted tweaks (127.0.0.1 DB URL,
      tasks.json runner, `tickers.txt`) — commit them
