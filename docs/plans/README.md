# Implementation plans — ML long-term stock picker

Sequential, session-sized plans for coding agents. Each plan is self-contained: a fresh
session should read [CLAUDE.md](../../CLAUDE.md) (hard rules), the plan, and the files it
links — nothing else is assumed. Do them in order; each builds on the previous one's
deliverables.

| # | Plan | Delivers | Depends on |
|---|---|---|---|
| 1 | [ml-01-yahoo-corporate-data.md](ml-01-yahoo-corporate-data.md) | Dividends, share counts, sector metadata, statement snapshots in Postgres | prices ingested |
| 2 | [ml-02-feature-pipeline.md](ml-02-feature-pipeline.md) | Shared versioned feature builder + `features` table; engine uses it | 1 |
| 3 | [ml-03-training-harness.md](ml-03-training-harness.md) | Dataset builder, LightGBM training, walk-forward validation, artifacts | 2 |
| 4 | [ml-04-backtest-integration.md](ml-04-backtest-integration.md) | `ml_lt` predictor/allocator on the backtest leaderboard | 3 |

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
