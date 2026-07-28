# Implementation plans

Session-sized plans for coding agents. Each plan is self-contained: a fresh session should read
[CLAUDE.md](../../CLAUDE.md) (hard rules), the plan, and the files it links — nothing else is
assumed.

- [`active/`](active/) — open or partially completed work
- [`completed/`](completed/) — landed work kept for its implementation or decision record

Every plan lives in one of those two directories; nothing stays loose in this one. When a plan
lands, move it to `completed/`, bump its relative links a level, and update the tables below.

## ML long-term stock picker

Do them in order; each builds on the previous one's deliverables.

| # | Plan | Delivers | Depends on |
|---|---|---|---|
| 1 | [ml-01-yahoo-corporate-data.md](active/ml-01-yahoo-corporate-data.md) | Dividends, share counts, sector metadata, statement snapshots in Postgres | prices ingested |
| 2 | [ml-02-feature-pipeline.md](active/ml-02-feature-pipeline.md) | Shared versioned feature builder + `features` table; engine uses it | 1 |
| 3 | [ml-03-training-harness.md](active/ml-03-training-harness.md) | Dataset builder, LightGBM training, walk-forward validation, artifacts | 2 |
| 4 | [ml-04-backtest-integration.md](completed/ml-04-backtest-integration.md) | `ml_lt` predictor/allocator on the backtest leaderboard | 3 |
| 5 | [ml-05-oos-backtest.md](completed/ml-05-oos-backtest.md) | Per-fold OOS backtest — the honest after-cost number the promotion rule needs | 4 |
| 6 | [ml-06-ridge-predictor.md](completed/ml-06-ridge-predictor.md) | `ml_lt_ridge` deployable (it beat LightGBM OOS) + LightGBM capacity cut | 4 (5 preferred) |
| 7 | [ml-07-forward-shadow.md](completed/ml-07-forward-shadow.md) | Monthly forward weight snapshots + realized-return report (paper-less forward test) | 4 — **land early, evidence accrues with time** |
| 8 | [ml-08-survivorship.md](completed/ml-08-survivorship.md) | Survivorship label on every run + delisted-data source decision prep | none |
| 9 | [ml-09-young-listings.md](completed/ml-09-young-listings.md) | **Optional** experiment: relax the 252-day listing floor, feature-set v2 | 5 (+6) — gated on the core surviving OOS |

ML-04's verdict was **not promoted** (its leaderboard win is in-sample for the deployed
artifact — see its completion notes). 5, 6 and 7 exist to produce honest evidence; do 5 first,
but 7 any time — every month it isn't running is forward evidence lost. 8 is independent.
9 only if the core strategy earns it.

Ground rules for every plan (from CLAUDE.md, restated because they bite here):

- Paper trading ceiling; never touch `assert_trading_allowed()` / `RiskChecker`.
- Signals/backtest code reads and writes **Postgres only** — no network calls.
- Yahoo requests go through the existing ≥2 s throttle; keep volume small.
- New tables: SQLAlchemy 2.0 typed style, `SqliteFriendlyBigInt` PKs, UTC timestamps,
  Alembic autogenerate + review + `alembic upgrade head`.
- `pytest`, `ruff check src tests`, `ruff format --check src tests`, `mypy src` green before done.
- Tick off the plan's checklist at the bottom of each file as items land; note follow-ups in
  [TODO.md](../../TODO.md).

Decisions already made (do not relitigate in-session; see
[multi-asset-roadmap.md](../multi-asset-roadmap.md) for the record):

- **Label:** 12-month forward total return in CAD, in excess of XEQT, converted to a
  cross-sectional percentile rank per rebalance date. Monthly sampling.
- **Universe:** ~180 names in [tickers.txt](../../tickers.txt), Yahoo-first ingestion
  (`tickers-yahoo.txt`); `.vscode/aggregate_tickers.py` keeps them in sync.
- **History:** short fundamental history (~4–5y from free yfinance) is accepted — the owner
  explicitly prefers recent-regime data over depth. Don't block on deeper archives; snapshot
  statements quarterly so history accrues forward.
- **News/sentiment:** deferred entirely for this track.
- **Model:** LightGBM (plus a trivial linear sanity baseline); no deep learning at this
  data size.

## Tooling track (2026-07)

Independent unless noted — pick by priority, one per session. Shared premise (do not
relitigate): **Postgres stays the system of record**; DuckDB/Polars/vectorbt are research
lenses only; anything feeding the real pipeline goes through `restore-*` into Postgres.

| # | Plan | Delivers | Priority / gate |
|---|---|---|---|
| 1 | [tools-01-finbert-sentiment.md](active/tools-01-finbert-sentiment.md) | FinBERT sentiment scorer (`[sentiment]` extra), `sentiment_model` provenance column, rescore CLI | **Do first** — most likely single prediction-quality win |
| 2 | [tools-02-pandera-validation.md](active/tools-02-pandera-validation.md) | pandera schemas gating price/feature frames + `check-data` CLI | Cheap insurance, any time |
| 3 | [tools-03-duckdb-archive-lens.md](active/tools-03-duckdb-archive-lens.md) | Read-only DuckDB lens over the Parquet archive (`[research]` extra) + Polars convention | When archive exploration is wanted |
| 4 | [tools-04-optuna-lgbm-search.md](active/tools-04-optuna-lgbm-search.md) | Optuna TPE+pruning replacing the LightGBM capacity grid in train.py | Before any wider hyperparameter space |
| 5 | [tools-05-alphalens-factor-report.md](active/tools-05-alphalens-factor-report.md) | Factor report (IC decay, quantiles, turnover) on **OOS** ml_lt predictions | Feeds the ML-05/06/07 promotion evidence |
| 6 | [tools-06-timescaledb.md](completed/tools-06-timescaledb.md) | price_bars hypertable + compression — **measurement gate first, expected to abort if unjustified** | Only if HOT-window disk pressure is real |
| 7 | [tools-07-deferred-bench.md](completed/tools-07-deferred-bench.md) | Decision record: MLflow / vectorbt / skfolio adoption triggers | Read before proposing any of those |
| 8 | [tools-08-mlflow-tracking.md](completed/tools-08-mlflow-tracking.md) | Optional local MLflow projection of authoritative training metadata | Triggered by Ridge joining as a second model family |

## Cross-project

| Plan | Delivers | Notes |
|---|---|---|
| [data-lake.md](active/data-lake.md) | This repo's `archive/` Parquet-on-R2 layer widened into a catalogued data layer other projects can read | Standalone. Paired with Carameli's `docs/plans/active/shared-devkit/plan-5-data-lake-otel.md` — that plan owns the shared client and OTel conventions, this one owns the schema, partitioning, and catalog |
