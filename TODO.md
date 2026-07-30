# TODO

Status of the build-out.
Keep this file updated as items land — check things off, don't delete them.
Larger, multi-session work lives in [docs/plans/](docs/plans/); this file is the backlog
and the pointer to it.

## 1 · Ingestion (fill the database)

- [ ] **IBKR historical connector** ([ibkr_historical.py](src/ibkr_trader/ingestion/market/ibkr_historical.py)) —
      ib_async, token-bucket throttle for official IBKR pacing rules; cache `conId` on instruments
- [ ] Questrade API connector — **candidate** (owner is a Questrade client; API free for
      clients, OHLC candles capped at 2 000/request, OAuth refresh-token flow). Evaluate as
      an official-API replacement for the Yahoo scraper if Yahoo starts blocking us.
- [ ] Shared connector plumbing: retry/backoff via tenacity, structured logging, per-source
      rate-limit config
- [ ] Extend `SqliteFriendlyBigInt` PK pattern to the remaining tables as their tests appear
      (now `price_bars` + `backtest_runs` + `news_articles` + `social_posts` + `trend_points`;
      still to do: `predictions`, `orders`, `executions`, …)

## 2 · Signals & features

- [x] ~~*`score_sentiment()` ([features.py](src/ibkr_trader/signals/features.py)) — start with*~~ [2026-07-17]
      VADER; persisted by [signals/sentiment.py](src/ibkr_trader/signals/sentiment.py)
      `score_pending` into `news_articles.sentiment` / `social_posts.sentiment` (only
      `sentiment IS NULL` rows → idempotent). Runs hourly via the `sentiment_score` serve job
      and manually via `ibkr-trader score-sentiment`. Tested (`tests/test_sentiment.py`).
- [x] ~~*Finnhub news history backfill*~~ [2026-07-17] — `finnhub_backfill` serve job (daily +
      on serve startup) walks company news backwards from each symbol's oldest stored article
      to a rolling floor (`FINNHUB_BACKFILL_DAYS`, default 365 — the free-tier depth), splits
      windows that hit the ~250-item response cap, budgets requests per run, self-heals with
      no state table (cursor = min(published_at) per symbol). Manual:
      `ibkr-trader ingest finnhub-backfill`. Tested (`tests/test_finnhub_backfill.py`).
      Next (blocked on ~1 yr of scored news): event-study IC check of sentiment vs forward
      1/5/21-day excess returns *before* adding any news feature to feature set v2.
- [ ] Ticker extraction: stoplist for WSB false positives (CEO/YOLO/DD…), validate against
      `instruments`; backfill `symbols` arrays on stored posts/articles
- [ ] **TOOLS-01 deferred:** Mention-count / mean-sentiment / trends-delta features — blocked on news/social
      ingestion (§1); fold into a future feature-set version when data exists
- [ ] `MomentumBaseline.predict()` ([predictor.py](src/ibkr_trader/signals/predictor.py)) +
      persist `predictions` rows (with feature snapshot) — body still stubbed (needs features)
- [ ] CLI command to run feature build + prediction for a date range

## 3 · Backtesting

**Monthly forward-shadow ritual:** run the VS Code task `Snapshot: Run Monthly`. It runs the price
ingestion chain first, then `snapshot run --all`; do not backfill a missed historical month.
Review matured evidence with `ibkr-trader snapshot report --horizon-months 1` (then 3/6/12 as
those horizons mature). Automating this cadence in `serve` is a follow-up, not part of ML-07.

- [ ] Validate cost model numbers against IBKR Canada's actual fee schedule (commission,
      spread/slippage bps) — currently conservative placeholders in `backtest/costs.py`
      (owner accepts the educated guesses for now, 2026-07-18).
- [x] ~~*USDCAD deep history + 2010 decision floor*~~ [2026-07-18] — `yahoo_fx.py` backfills
      the same CASH instrument from Yahoo (`ingest fx --source yahoo`, coverage 2003-09-17+;
      `poll_fx` keeps both providers current; connector clamps Yahoo's occasionally
      inconsistent high/low). `backtest run --eval-start` starts decisions at a date (owner
      floor: 2010-01-04) while earlier bars warm up features. Tested
      (`tests/test_yahoo_fx.py`, scheduler/CLI/engine tests).
- [x] ~~*Equity curves persisted + Streamlit dashboard*~~ [2026-07-18] — every persisted run
      stores `equity_curve`/`benchmark_equity_curve` in `backtest_runs.metrics`;
      `ibkr-trader dashboard` (`[dashboard]` extra) serves leaderboard + equity/drawdown
      charts + a run launcher. VS Code tasks: `backtest: run`, `backtest: run (ETF floor,
      2010+)`, `backtest: compare`, `backtest: oos`, `train: run`, `dashboard: up`. Tested
      (`tests/test_dashboard_data.py`, `tests/test_dashboard_app.py` via streamlit AppTest).
- [x] ~~*Streamlit → static HTML report*~~ [2026-07-19] — the resident Streamlit server was
      too heavy for the 16 GB laptop (a leftover `streamlit run` survived a VS Code crash at
      90% RAM). Replaced by `ibkr-trader report` (`[report]` extra, plotly only): renders
      `backtest_runs` to one self-contained `report.html` (leaderboard + interactive
      equity/drawdown charts) and exits — nothing stays resident. Run backtests via
      `ibkr-trader backtest run`, then regenerate. Streamlit dep, `dashboard/app.py`, the
      `[dashboard]` extra and the vendored skill are gone; `dashboard/data.py` unchanged.
      VS Code task: `report: generate`. Tested (`tests/test_dashboard_report.py`, CLI tests).
- [x] ~~*ETF-floor universe (survivorship lower bound)*~~ [2026-07-18] — `tickers-etfs.txt`,
      24 broad-market ETFs (deep histories: SPY 1993, XIU 1999; US-listed ETFs added to
      `tickers-yahoo.txt` + aggregate). Quote ETF-floor results beside curated-stock upper
      bounds; real fix (PIT membership + delisted-data provider) still open below.
- [ ] After choosing a delisted-data provider, add `Instrument.end_of_life_date` (and provider
      identifier/symbol-history fields if its contract requires them), then ingest dead tickers
      as ordinary instruments whose daily bars stop at their final trading date.
- [ ] Fundamental solvency screen (market cap, distress/default) once fundamentals are ingested;
      precise per-security dividend cashflows to replace the yield-based withholding drag.

## 4 · IBKR paper trading

**Human prerequisites (can't be automated):**

- [ ] Request paper account in Client Portal; note the `DU…` username → `.env`
- [ ] Decide market data: free delayed vs. share live subscriptions with paper (24 h to apply)
- [ ] First `docker compose --profile ibkr up -d` login + 2FA approval; plan for the ~weekly
      Sunday re-auth

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

- [ ] Pick host (Canadian region preferred);
      provision Docker VM
- [ ] Secrets management (not `.env` on disk), Postgres backups, log shipping
- [ ] Alerting: broker disconnected / 2FA needed / job failures (email or push)
- [ ] CI: GitHub Actions running ruff + mypy + pytest

## Deferred tooling follow-ups (TOOLS-01–08)

- [ ] **TOOLS-01:** Run FinBERT download and rescore end-to-end against the dev database.
- [ ] **TOOLS-01:** Review a real FinBERT score sample, rescore the backlog, then flip `.env`.
- [ ] **TOOLS-01:** Evaluate richer article/body text if headline-only sentiment proves weak.
- [ ] **TOOLS-01:** Evaluate FinBERT GPU or quantization only if CPU volume becomes a bottleneck.
- [ ] **TOOLS-02:** Reassess Great Expectations/dbt-style profiling if Pandera proves insufficient.
- [ ] **TOOLS-02:** Add cross-table referential data-quality checks if integrity gaps appear.
- [ ] **TOOLS-02:** Add data-quality alerting for failed schema checks.
- [ ] **TOOLS-02:** Consider an explicit bad-row repair workflow; keep automatic repair off by default.
- [ ] **TOOLS-03:** Validate `archive query` against the real R2 archive and confirm pushdown.
- [ ] **TOOLS-03:** Add research-only feature engineering on the DuckDB archive lens when needed.
- [ ] **TOOLS-03:** Use Polars for a future heavy dataframe workload; do not rewrite working pandas.
- [ ] **TOOLS-04:** Widen Optuna's search space to learning rate, regularization, and feature fraction.
- [ ] **TOOLS-04:** Evaluate multi-objective Optuna search if one IC objective becomes inadequate.
- [ ] **TOOLS-04:** Add persistent Optuna storage/dashboard only if trial-level browsing is needed.
- [ ] **TOOLS-04:** Benchmark Optuna versus grid end-to-end on the real dataset.
- [ ] **TOOLS-05:** Run the factor report on real OOS predictions and save its evidence summary.
- [ ] **TOOLS-05:** Add sector-exposure analysis once reliable point-in-time sector data exists.
- [ ] **TOOLS-06:** Add Timescale continuous aggregates only when repeated aggregate queries justify them.
- [ ] **TOOLS-06:** Reassess Timescale retention policies only if archive-owned deletion changes.
- [ ] **TOOLS-06:** Size and validate TimescaleDB for production/cloud deployment when that starts.
- [ ] **TOOLS-07:** Add vectorbt for read-only coarse screening when hundreds of variants need sweeping.
- [ ] **TOOLS-07:** Add skfolio after a predictor passes OOS and forward shadow and reaches paper.

## Shared data lake ([plan](docs/plans/active/data-lake.md))

Phases 0 (infra), 1 (catalog), 1.5 (models split + config inversion) and 1.75 (session inversion)
are done — **prep and infrastructure are both complete, Phase 2 is unblocked**. R2 bucket
`ibkr-trader` is wired in `.env` and verified reachable (empty); repo
[alexandrec90/data-lake](https://github.com/alexandrec90/data-lake) created private 2026-07-29.

- [x] ~~*R2 bucket renamed to `data-lake`*~~ [2026-07-29] — created via the Cloudflare REST API with
      a short-TTL user API token while the bucket was still empty (free). Public access verified
      **disabled** on both buckets (no `r2.dev` domain, no custom domains). Old `ibkr-trader` bucket
      is empty and unused — safe to delete.
- [x] ~~*First real archive run, end to end*~~ [2026-07-29] — `archive raw --min-age-days 13` →
      1 505 payloads in `raw/news_articles/2026-07.parquet`, catalog manifest written, and the
      DuckDB lens reads it back from R2 (the Phase 4 reuse contract, proven). 0 rows lost title or
      sentiment. **Not `archive bars`:** every stored bar is `1 day`, which it refuses by design, so
      it writes nothing until the IBKR intraday connector exists.
- [ ] **Owner:** fix the stale `ARCHIVE_S3_BUCKET=ibkr-trader` exported in your terminal session —
      it outranks `.env`, so `.env` edits are silently ignored (restarting the shell/editor clears
      it). Check with `env | grep ARCHIVE_`. Cost real debugging time; see
      [remote-archive.md](docs/operations/remote-archive.md) "Gotcha".
- [ ] **Owner:** delete the bootstrap `CLOUDFLARE_API_TOKEN` from `.env` + Cloudflare (account-level
      R2 Edit; self-expires 2026-08-05). Consider rotating the R2 object token — its **access key
      ID** (not the secret) was printed to a session transcript on 2026-07-29.
- [ ] Delete the now-unused empty `ibkr-trader` bucket (needs an account-scoped token; the current
      object token is `data-lake`-only).
- [ ] Finish archiving the remaining ~280 666 scored payloads: rerun
      `archive raw --min-age-days 0` (idempotent — merges what's already there). A first attempt on
      2026-07-29 was interrupted part-way: R2 kept 5 partitions (64 777 catalogued rows) but the
      local NULLs rolled back, since the whole run commits in one transaction. Data exists in both
      places, so nothing is at risk — only ~0.9 MiB of re-upload. Needs the `db` container up.
- [ ] Phase 2 (Claude, fresh session): move `ingestion/`, `archive/`, `db/base.py`,
      `db/lake_models.py`, `lens` into the `data-lake` package; swap `archive/`'s `Settings`
      annotations for a Protocol the package owns; `ibkr_trader` becomes a consumer.
- [ ] Phase 3: GitHub Actions cron writes Parquet → R2 (IBKR pacing lives in the runner).

## Housekeeping

- [ ] Re-check official/current IBKR and deployment details as each area gets touched
      (gnzsnz env-var names before first gateway run; TSX exchange naming before trading CAD)
- [ ] `mypy src` isn't clean-guaranteed yet — run and fix once implementations start landing

## Done

- [x] ~~***NewsAPI connector** ([newsapi.py](src/ibkr_trader/ingestion/news/newsapi.py)) —*~~ [2026-07-16]
      pagination, upsert on `(source, external_id=hash(url))`, set `fetched_at`
- [x] ~~*Alpha Vantage / Finnhub candles — **optional**, only if FMP+Yahoo coverage proves*~~ [2026-07-16]
      insufficient (free tiers are tight; verify Finnhub candle access first)
- [x] ~~*Daily price refresh in `serve`*~~ [2026-07-17] — `prices_poll` job (daily + on
      startup): incremental Yahoo bar fetch for every yahoo-tracked instrument
      (`tracked_yahoo_symbols`, universe + XEQT) plus FX pairs (`FX_PAIRS`, default USDCAD)
      from the newest stored bar. The `app` compose service (profile `app`,
      `restart: unless-stopped`, universe files bind-mounted) now runs `serve` persistently —
      start Docker Desktop and everything ingests/scores/prunes itself.
- [x] `serve` command ([cli.py](src/ibkr_trader/cli.py) → [scheduler.py](src/ibkr_trader/scheduler.py)) —
      `BlockingScheduler` (UTC) with interval jobs: Reddit poll, Finnhub-news poll (iterates
      `NEWS_UNIVERSE_FILE`, spaces calls under the free 60/min limit, skips a failing symbol),
      Google Trends poll (no-op without `TRENDS_KEYWORDS`), and a raw-prune job. Each job is
      `_guard`-wrapped so one failure logs and the scheduler survives; cadence/spacing come from
      `Settings`. `build_scheduler()` returns the unstarted scheduler for testability. Tests:
      `tests/test_scheduler.py`. **Still TODO:** daily FMP/price refresh job (prices out of
      scope for this pass) and the trading loop (stays out until §3/§4 justify it).
- [x] **Raw-payload pruning** ([maintenance.py](src/ibkr_trader/maintenance.py) `prune_scored_raw`) —
      NULLs the `raw` blob on `news_articles`/`social_posts` rows that signals has already
      sentiment-scored (`sentiment IS NOT NULL`), with a `min_age_days` grace knob; forces a
      real SQL NULL via `null()` (plain None on a JSON column stores JSON 'null' and reclaims
      nothing). Idempotent, never deletes rows. Wired as the `prune_raw` `serve` job. Tests:
      `tests/test_maintenance.py`.
- [x] **Finnhub company-news connector** ([finnhub_news.py](src/ibkr_trader/ingestion/news/finnhub_news.py)) —
      `ingest finnhub-news <SYM> [--date-from --date-to]` (default: last 7 days), upserts
      `NewsArticle` on `(source, external_id=str(item.id))`, tags the queried symbol into
      `symbols` (merges when an article surfaces under a second ticker), trimmed `raw` (drops
      image URLs), sanitizes provider HTTP errors (key never leaked). Preferred over NewsAPI:
      articles arrive ticker-tagged and the free tier gives ~1yr history.
      Tested (`tests/test_finnhub_news_connector.py`).
- [x] **Reddit connector** ([reddit.py](src/ibkr_trader/ingestion/social/reddit.py)) — PRAW
      `.new(limit=)` over the 4 subreddits from `Settings.subreddits`, author **hashed only**
      (deleted authors → NULL), re-poll updates score/num_comments/body (keeps first-seen
      `created_at`), trimmed `raw` (never `vars(submission)`), client behind `_reddit_client`
      so tests never import praw or hit the network. Tested (`tests/test_reddit_connector.py`).
- [x] **Google Trends connector** ([google_trends.py](src/ibkr_trader/ingestion/social/google_trends.py)) —
      pytrends, module-level 60 s throttle, pinned `timeframe="now 7-d"` so points stay
      comparable across fetches, skips `isPartial` provisional buckets, upserts `TrendPoint`
      on `(keyword, geo, ts)`, ≤5 keywords/payload, client behind `_trends_client`.
      Tested (`tests/test_google_trends_connector.py`).
      **Batch mode via symbol↔search-term mapping** (`trends-keywords.txt`, 24 tickers /
      34 lines in three signal families: `<TICKER> stock` investor attention for all,
      brand attention for unambiguous non-navigational consumer names, product demand
      for stable flagship products (iPhone); multiple lines per ticker supported):
      `ingest trends --mapping-file` / VS Code task runs ONE keyword per request
      (own 0-100 scale — multi-keyword payloads normalize against the batch max) on the
      60 s throttle, default `today 5-y` weekly so the first run doubles as the backfill;
      `serve` trends job prefers the mapping file (`TRENDS_MAPPING_FILE`) over
      `TRENDS_KEYWORDS`. Downstream features must be scale-invariant (deltas/z-scores) —
      re-fetches renormalize the index. Do NOT dump the 180-name universe in the mapping
      file: bare tickers are search noise and each line costs ~1 min/run.
      **X/Twitter deliberately skipped** — no usable free read tier (~$100–200/mo Basic), plus
      ToS/QC-privacy risk. Revisit GDELT (free, global news tone) only if Finnhub+Reddit prove
      thin — it's high-volume, watch the 16 GB disk.
- [x] Project skeleton: config (paper-by-default gate), DB models, Alembic (initial migration
      applied), Docker compose (Postgres 5433 + opt-in ib-gateway), CLI, CLAUDE.md, tasks.json
- [x] IBKR research pass + Québec legal notes + data-source survey — written up in
      [docs/reference/ibkr/](docs/reference/ibkr/), [docs/reference/legal-quebec-canada.md](docs/reference/legal-quebec-canada.md),
      and [docs/reference/data-sources.md](docs/reference/data-sources.md). All three are point-in-time research:
      re-check official/current sources before changing those areas.
- [x] Multi-asset implementation roadmap completed — [docs/multi-asset-roadmap.md](docs/multi-asset-roadmap.md)
- [x] **FMP price connector end-to-end** — `ingest prices <SYM> --source fmp`, upserts
      `price_bars`, handles `.TO` symbols, tested (`tests/test_fmp_connector.py`);
      `tickers.txt` universe + VS Code ingest tasks
- [x] **Registered-account long-term strategy — vertical slice**:
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
- [x] **`build_daily_features()` — shared, versioned feature pipeline (ML-02)**
      ([features.py](src/ibkr_trader/signals/features.py)): pure core `build_features_asof`
      (`FEATURE_SET_VERSION="1"`: returns 1/3/6/12m, `momentum_12_1`, `volatility`
      (semantics identical to the old engine version) + 60d, downside deviation, 252d max
      drawdown, % off 52w high, volume z-score 60d, excess returns vs benchmark, dividend
      yield TTM / 3y growth, log market cap; `sector` string in the persisted payload only)
      + thin DB wrapper `build_daily_features(session, instrument_ids, dates)` upserting
      `features` snapshots keyed `(instrument_id, ts, feature_set_version)`. Engine
      `_features_asof` now delegates to the core — ML-01 corporate data used when ingested,
      price-only degradation otherwise; `momentum_lt` 2015→2025 dev-DB metrics verified
      **byte-identical** pre/post refactor. Migration `d4e5f6a7b8c9` applied. Tests:
      `tests/test_features.py` (no-look-ahead property, legacy parity, upsert idempotency).
- [x] **Training harness (ML-03)** — supervised dataset + walk-forward validation + LightGBM:
      [dataset.py](src/ibkr_trader/signals/dataset.py) (`build_dataset`: one row per
      (instrument, month-end), feature set v1 + eligibility screen as-of t, label = 12m forward
      CAD total return in excess of XEQT percentile-ranked per date; USD names convert through
      the stored USDCAD series; rows without a full 12m forward window excluded),
      [validation.py](src/ibkr_trader/signals/validation.py) (expanding walk-forward with a
      **12-month purge**, Spearman rank IC per test date; no random splits),
      [train.py](src/ibkr_trader/signals/train.py) (LightGBM + ridge sanity floor, versioned
      artifacts `models/ml_lt/<vN>/` with LightGBM + ridge models, metadata.json + `latest`
      marker; sector = LightGBM native categorical). CLI `train run` / `train report`; `[ml]` extra
      (lightgbm, scikit-learn) — core package imports without it. Tests: `tests/test_dataset.py`,
      `tests/test_validation.py`, `tests/test_train.py` (purge, no-look-ahead, rank-label
      uniformity, end-to-end smoke). First real run (180 names, 2019-08→2025-07 labels,
      6 folds): OOS rank IC lightgbm +0.035 ±0.112, ridge +0.121 ±0.168 — decision metric
      stays the after-cost backtest (ML-04).
- [x] **Predictor registry** ([predictor.py](src/ibkr_trader/signals/predictor.py)) —
      `@register` / `get_predictor` / `available`; `MomentumBaseline` registered so models
      (long-term vs short-term, ML vs baseline) resolve by name. Tests:
      `tests/test_predictor_registry.py`
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
- [x] Baselines: `buy_and_hold` (XEQT) and `equal_weight` allocators, plus `momentum_lt`, so
      models have an honest benchmark (the engine runs buy-and-hold alongside every strategy).
      `equal_weight` is a 15-name book (v2): the old 1/20 = 5% target equalled the 5% rebalance
      band exactly, so it never traded.
- [x] **ML-04 — `ml_lt` wired into the leaderboard**
      ([predictor.py](src/ibkr_trader/signals/predictor.py) `MlLongTerm` + registered
      `MlLtAllocator` in [portfolio.py](src/ibkr_trader/signals/portfolio.py), top-15/20%-cap
      like `momentum_lt`): resolves the newest artifact via `ML_LT_MODEL_DIR`
      (default `models/ml_lt/`), score = predicted rank − 0.5, refuses to score on a
      feature-set-version mismatch (loud log), clear errors without `[ml]`/artifact; every
      run now pins `feature_set_version` (+ `model_version` = artifact version) into
      `backtest_runs.params`. Tests: `tests/test_ml_predictor.py`. Leaderboard
      2021-08-02→2026-07-07 (window bounded by USDCAD coverage starting 2021-07-08), tfsa+rrsp,
      by Sharpe: `ml_lt` v1 1.61 (+517%, DD −27.5%) > `momentum_lt` 1.45 (+228%, −15.7%) >
      `equal_weight` 1.38 (+122%) > `buy_and_hold` 1.22 (+98%). **Promotion verdict: NO** —
      v1 was fit on the full labeled window, so this backtest is in-sample for the deployed
      booster; the honest OOS evidence is the fold ICs (lightgbm +0.035 ±0.112, under ridge's
      +0.121) and cannot support a 44% CAGR. Universe is survivorship-biased. Before §4 wiring:
      per-fold OOS backtest (done — ML-05, below) and/or paper-forward evaluation
      (see docs/plans/completed/ml-04-backtest-integration.md completion notes).
- [x] **ML-05 — per-fold OOS backtest (the honest number)**
      ([backtest/oos.py](src/ibkr_trader/backtest/oos.py) `FoldSwitchingAllocator` +
      `run_oos_backtest`, CLI `backtest oos`, additive `RegisteredStrategyConfig.eval_start`
      + `Allocator.asof` hook + `BacktestEngine.run_allocator`): one model per walk-forward
      fold trained in memory, every decision at t from a model whose labels were realized
      before t (leakage guard at construction and per decision), baselines on identical
      decision dates. Stitched OOS span 2022-08-31→2025-07-31 (6 folds), TFSA, after costs:
      `ml_lt_ridge_oos` +147% (CAGR 35.7%, Sharpe 1.48, DD −25.9%, 31.7 tr/yr) >
      `ml_lt_oos` +140% (34.4%, 1.46, −18.9%) > `momentum_lt` +98% (25.9%, 1.46) >
      `equal_weight` +88% (23.8%, **1.68**) > `buy_and_hold` +64% (18.1%, 1.36).
      **Verdict: qualified yes — on returns; ridge is the candidate.** Both families beat
      `momentum_lt` + `buy_and_hold` after costs, and ridge is self-consistent with its fold
      ICs (+0.121) — but Sharpe merely ties `momentum_lt` and trails `equal_weight`, ridge
      takes the deepest drawdown, and the universe stays survivorship-biased over one ~3y
      regime. No paper wiring off this alone: do ML-06 (`ml_lt_ridge` deployable) + ML-07
      (forward shadow) first; `momentum_lt` remains the reference strategy
      (see docs/plans/completed/ml-05-oos-backtest.md completion notes). Tests: `tests/test_oos.py`,
      `eval_start` guards in `tests/test_engine.py`.
- [x] **ML-06 — deployable ridge + LightGBM capacity cut**
      ([train.py](src/ibkr_trader/signals/train.py), [predictor.py](src/ibkr_trader/signals/predictor.py),
      [portfolio.py](src/ibkr_trader/signals/portfolio.py)): every artifact now saves
      `ridge.joblib` plus numeric columns and scikit-learn version (major/minor mismatch refuses
      load with a retrain hint); registered `ml_lt_ridge` uses the same top-15/20%-cap contract
      and feature-version guard as `ml_lt`. The fold-IC-only 27-candidate LightGBM grid selected
      7 leaves / 50 min-child / 100 estimators (mean fold IC +0.0916, std 0.0830), now the
      default; artifact `v3` stores the full grid record. Stitched OOS 2022-08→2025-07, TFSA:
      regularized `ml_lt_oos` +245.7% (Sharpe 1.74, DD -24.7%) > ridge +147.1% (1.48,
      -25.9%) > momentum +97.7% > equal-weight +88.1% > buy-and-hold +63.6%; RRSP was
      +247.3% vs +148.0%. Ridge used 94 trades versus LightGBM's 220. Full-artifact runs also
      completed for TFSA/RRSP but remain in-sample (see plan completion notes). Verdict:
      capacity-cut LightGBM leads this historical OOS rerun, ridge remains the lower-turnover
      alternative; neither is live-trading authorized. Tests: `tests/test_train.py`,
      `tests/test_ml_predictor.py`.
- [x] **ML-07 — broker-free forward shadow snapshots** — current-date-only target weights,
      idempotent daily upserts, stale-data warning, realized CAD return vs XEQT after approximate
      turnover costs, and the monthly ingestion → snapshot VS Code task. No orders or network.
- [x] **ML-09 — young-listings experiment:** feature set v2 adds as-of `history_days`; the
      history floor is a validated per-run CLI choice on training, ordinary backtests, and OOS
      backtests (default remains 252 and every run pins it). Same-seed stitched OOS at floor 63
      versus 252 rejected the lower floor: LightGBM +252.2% / Sharpe 1.60 / DD −32.4% versus
      +261.4% / 1.67 / −30.4%; ridge +107.8% versus +106.3% with ~−31% DD. The lower floor
      added only 9 dataset rows, exposing the static universe as the real young-listing gate.
      Keep 252; quarterly public-listing intake is documented in the strategy doc. Private
      companies remain out of scope. See [completion notes](docs/plans/completed/ml-09-young-listings.md).
- [x] Guardrail tests: no look-ahead (fill at open t+1; features/eligibility use data ≤ t),
      trade-budget cap, FX-to-CAD, dividend withholding, and a survivorship label/warning on
      every run (IBKR has no delisted history).
- [x] Multi-provider stores don't double-count: `_load_series` picks exactly one `source` per
      instrument/bucket (widest window coverage wins, `SOURCE_PREFERENCE` breaks ties) — safe
      to mix providers across symbols (e.g. FMP for US names, Yahoo for XEQT). Tested in
      `tests/test_engine.py`.
