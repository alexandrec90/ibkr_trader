# Multi-Asset Model Evaluation Roadmap

Goal: compare how well models make money across supported investment types while reusing the
same stored data, feature pipeline, backtest engine, audit trail, and IBKR paper-trading rails.
All execution remains paper-only; every order path must keep `Settings.assert_trading_allowed()`
and `RiskChecker.check()`.

> **Current priority — registered-account long-term track.** The first concrete strategy built
> on this core is the tax-sheltered, low-turnover, quality-only allocator for RRSP/TFSA/FHSA/LIRA
> accounts: see [registered-account-strategy.md](registered-account-strategy.md). It lives inside
> Phase 1 (stocks/ETFs) — a portfolio-weights `Allocator` layer, an eligibility screen, a
> per-account trade budget, a churn-penalizing cost model with CAD/FX + US-dividend withholding,
> and a simulator that reports "how much you'd have made after costs and tax vs buy-and-hold."
> The multi-asset generalization below remains the longer arc; the allocator/eligibility/cost
> machinery is asset-class neutral and carries forward.

## Decisions taken 2026-07 (ML long-term picker)

Implementation sequence for these lives in [plans/](plans/README.md) (ML-01 … ML-04, sized
for one coding-agent session each); the decisions themselves are settled:

- **Label:** 12-month forward total return in CAD, excess of XEQT, cross-sectional
  percentile rank per monthly rebalance date.
- **Universe & sourcing:** ~180 names (TSX + US large caps + CAD core ETFs), Yahoo-first
  ingestion; FMP demoted to FX + backup. `tickers.txt` is generated from the per-connector
  lists by `scripts/aggregate-tickers.py`.
- **Fundamentals:** free yfinance only — statements are ~4–5y deep (probed 2026-07), which
  the owner accepts as a deliberate recency-over-depth bet; snapshot statements quarterly so
  point-in-time history accrues forward. Dividends/share counts/earnings dates are deep and
  usable immediately.
- **News/sentiment:** deferred for this track (horizon mismatch + no affordable archive);
  revisit for the short-term track once collectors have accrued history.
- **Model:** LightGBM + linear sanity floor; walk-forward validation with 12-month purge;
  selection by after-cost leaderboard, not prediction error.

## Guiding architecture

The reusable core should stay asset-class neutral:

- `instruments` is the canonical identity for anything tradable or backtestable.
- `price_bars` stores normalized OHLCV by `instrument_id`, `bar_size`, `source`, and
  `what_to_show`.
- `news_articles`, `social_posts`, and `trend_points` are shared alternative-data inputs.
- `features` should produce time-indexed feature snapshots for an `instrument_id`.
- `predictions`, `backtest_runs`, `orders`, and `executions` remain the audit trail.

Product-specific behavior belongs in thin layers around that core:

- contract resolution and IBKR `conId` caching;
- data-source symbol mapping;
- feature variants;
- cost, margin, sizing, expiry, and assignment logic;
- execution contract building and risk checks.

## Phase 1: Make stocks/ETFs the reference implementation

1. Finish wide daily ingestion for the current equity universe:
   FMP first, optional Alpha Vantage/Finnhub only if coverage is missing.
2. Implement text/social/trends ingestion and ticker/entity extraction.
3. Build daily feature snapshots:
   returns, volatility, volume z-score, mention counts, mean sentiment, trends delta, market
   regime features, and sector/benchmark-relative returns where available.
4. Implement the backtest engine with no look-ahead:
   signal at close `t`, fill at open `t+1`, explicit costs/slippage, benchmark comparison.
5. Add a model leaderboard:
   rank by CAGR, Sharpe, max drawdown, hit rate, turnover, and after-cost return.
6. Finish IBKR paper connectivity and stock order plumbing:
   qualify contract, `whatIf`, risk checks, order persistence, execution reconciliation.

This becomes the known-good template for every later product family.

## Phase 2: Generalize instrument and contract identity

Add a product-aware contract layer before adding derivatives or fixed income:

- extend or supplement `instruments` with normalized fields:
  `sec_type`, `primary_exchange`, `local_symbol`, `trading_class`, `multiplier`,
  `expiry`, `strike`, `right`, `underlying_instrument_id`, `last_trade_date`,
  `min_tick`, `price_magnifier`, and metadata JSON;
- add a provider-symbol mapping table if multiple vendors disagree on symbols;
- add an IBKR contract resolver service:
  input canonical identity, call `reqContractDetails` / `qualifyContracts`, cache `conId`,
  reject ambiguous matches;
- make `OrderRequest` identify an `instrument_id` or resolved contract, not only a stock symbol;
- update market ingestion to fetch by resolved contract when source is IBKR.

Deliverable: the rest of the app can ask for "instrument X" without knowing whether it is a
stock, future, option, warrant, bond, forex pair, combo, or event contract.

## Phase 3: Product-family rollout order

### 3A. ETFs and leveraged/inverse ETPs

Closest to stocks. Reuse equity ingestion, bars, sentiment, features, backtests, and order path.
Add product flags and stricter risk caps for leverage/inverse exposure.

### 3B. Forex

Moderate difficulty. Add `CASH` contract support, currency-pair identity, FX-specific bars, and
CAD reporting conversion. Features can reuse macro/news/trends and price momentum, but sizing
must be pip/notional-aware.

### 3C. Futures

Moderate to high difficulty. Add contract-month discovery, continuous-series construction for
research, roll rules, multiplier-aware PnL, margin-aware risk, and product calendars. Trade only
specific `FUT` contracts; continuous futures are for historical research only.

### 3D. Options, warrants, and futures options

High difficulty. Add chain discovery, expiry/strike/right identity, implied volatility and Greeks
where available, option-specific costs, exercise/assignment/expiry handling, and strategy-level
backtests. Start with simple long premium strategies before spreads or short options.

### 3E. Bonds / fixed income

High difficulty. Add fixed-income identifiers, quote conventions, accrued interest, yield/duration
features, sparse-liquidity handling, and conservative execution checks.

### 3F. Combos and multi-leg strategies

Build only after single-leg options/futures are reliable. Add `BAG` contract support, leg tables,
strategy-level orders, combo pricing, per-leg fills, and risk checks on aggregate exposure.

### 3G. Prediction/event contracts

Special workflow. Add event-contract discovery and metadata, model settlement outcomes explicitly,
and evaluate probability calibration as well as PnL. Eligibility and contract definitions must be
verified in IBKR docs/TWS before implementation.

## Phase 4: Shared model-evaluation framework

Create a common experiment contract for all product types:

- universe definition: product family, symbols/contracts, date range, liquidity filters;
- label definition: next-period return, risk-adjusted return, probability of profit, or event
  settlement outcome;
- feature set version: shared features plus product-specific features;
- execution model: costs, slippage, spread, margin, borrow, expiry/roll assumptions;
- portfolio construction: ranking, target weights, caps, rebalance frequency;
- metrics: after-cost return, drawdown, Sharpe/Sortino, turnover, hit rate, exposure, capacity,
  tail loss, calibration where relevant.

Persist all of this into `backtest_runs.params` so leaderboards compare like with like.

## Phase 5: Paper-trading validation per product family

Only promote a product family to paper execution after its backtests pass:

1. contract qualification succeeds and caches `conId`;
2. delayed/live quote retrieval works;
3. `whatIf` margin/commission preflight works;
4. product-specific `RiskChecker` rules pass;
5. order persistence and fill reconciliation are tested;
6. paper fills are compared with simulated fills to calibrate slippage/cost assumptions.

## Suggested near-term sequence

1. Complete the current stock/ETF ingestion, feature, and backtest TODOs.
2. Add the contract resolver and richer instrument schema.
3. Expand to leveraged/inverse ETPs and forex.
4. Add futures research with continuous-series backtests, then specific-contract paper tests.
5. Add options/warrants/FOP after the core backtester handles expiry-aware instruments.
6. Add bonds and event contracts only after the contract layer and risk engine are mature.
