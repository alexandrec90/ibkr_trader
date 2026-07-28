# Registered-Account Long-Term Strategy

> ⚠️ Engineering design, not financial or tax advice. See
> [legal-quebec-canada.md](reference/legal-quebec-canada.md) and confirm anything consequential with a
> CPA / Québec securities professional. `[verify]` markers flag facts to confirm.

## Goal

Maximize CAD growth and minimize risk **inside Canadian registered accounts** (RRSP, TFSA,
FHSA, LIRA), where the tax shelter only holds if you *invest* rather than *carry on a business*.
So this is deliberately **not** a short-term price-prediction system. The model makes long-term
**allocation decisions**, and the backtester answers one question:

> *If I had followed this model in this account, how much money would I have made — after every
> real-world cost (commissions, slippage, spread, turnover, FX, and dividend withholding tax)?*

## Why long-term / low-turnover (the legal core)

Day-trading inside a TFSA is deemed **business income** by CRA and draws audits
([legal doc §Tax](reference/legal-quebec-canada.md#tax-the-part-most-likely-to-bite)). The defence is
*frequency and intent*: a low-turnover, quality-only, long-only, buy-and-hold approach is the
normal, intended, tax-advantaged use of these accounts. Every hard constraint below exists to
stay on the right side of that line **and** because it's simply better long-term investing.

## Hard constraints (enforced, not advisory)

| Constraint | Where | Rule |
|---|---|---|
| Eligible assets only | [`signals/eligibility.py`](../src/ibkr_trader/signals/eligibility.py) | No penny stocks (min price), liquidity floor, ≥1y listing history by default (pinned per run), major CAD/USD listings only, **no leveraged/inverse/volatility ETPs**. |
| Long-only, capped | [`signals/portfolio.py`](../src/ibkr_trader/signals/portfolio.py) | Weights ≥ 0, sum ≤ 1, per-name concentration cap. No shorting, no margin. |
| Trade budget | [`backtest/engine.py`](../src/ibkr_trader/backtest/engine.py) | Hard **per-account** annual cap (default **100** trades/yr, buys + sells). A backstop — the cost function keeps real turnover far below it. |
| Cost function does the work | [`backtest/costs.py`](../src/ibkr_trader/backtest/costs.py) | Commission + slippage + spread **+ a churn penalty** on turnover **+ a CAD↔USD conversion spread** when the currency mix shifts. Raising the churn penalty makes small rebalances not worth it long before the hard cap bites. |

The screen is also intended to gate live orders later (execution.risk) so paper/live can never
transmit an ineligible or over-budget order.

## Universe

**ETFs + screened blue-chip stocks.** Start from broad, liquid ETFs (e.g. `XEQT`, `VGRO`,
`XBB`) plus large-cap names that clear the eligibility screen. Fundamental solvency screening
(market cap, distress/default risk for individual names) is a **documented next step** — it
needs fundamentals ingested first; until then the price + liquidity + history + curated-universe
proxy carries the "nothing likely to default" intent. Populate `tickers.txt` accordingly.

The practical new-listing gate is the static source list, not just the history screen. Review
new public listings quarterly (and after a specifically noteworthy IPO): once daily market data
and the basic instrument metadata are available, add the provider-formatted symbol to
`tickers-yahoo.txt`, run `Ingest: Run Source` twice (Yahoo prices, then Yahoo fundamentals), then
run `Tickers: Aggregate Universe (tickers.txt)`. Do not hand-edit the aggregate as the source of
truth. A listing enters a strategy only after it also clears that run's `min_history_days` and
the other eligibility rules. Private/pre-IPO companies such as SpaceX have no public daily bars
or tradable listing and are entirely out of scope.

Because `tickers.txt` is curated from securities that are listed today, every absolute excess
return produced from it is an **upper bound**, not an unbiased performance estimate. The
defensible comparison is the ranking of strategies run over the identical universe and window;
absolute return levels from this universe must not be used to claim achievable performance.

## Accounts & tax ([`accounts.py`](../src/ibkr_trader/accounts.py))

All five share one engine; only the **US-dividend withholding** treatment differs (Canadian
dividends are never withheld). The same US-heavy allocation therefore nets differently by
account — the leaderboard shows the tax drag.

| Account | US-dividend withholding | Notes |
|---|---|---|
| RRSP | 0 (treaty-exempt, held directly) | Taxed on withdrawal (not modelled). |
| LIRA | 0 (treated like RRSP `[verify]`) | Locked-in. |
| TFSA | 15%, non-recoverable | Prefer CAD-domiciled US exposure here. |
| FHSA | 15% (`[verify]` treaty status) | Modelled like TFSA, conservative. |
| Non-reg | 15% but recoverable (FTC) → ~0 drag | Baseline; business-income risk lives here too. |

**Simplification:** headline P&L is the **pre-withdrawal account value in CAD**. Withdrawal tax
(RRSP/LIRA) is *not* modelled — the goal is to maximize each account's balance.

## The model (decision-optimized)

The decision interface is `Allocator.allocate(candidates, features) -> {instrument_id: weight}`.
Models are selected by **net-of-cost simulated P&L**, not price-prediction error — decision-
optimized by construction. Provided today:

- `equal_weight`, `buy_and_hold` (XEQT) — honest baselines (equal weight holds the same
  15-name book size as the model strategies so the leaderboard compares like with like).
- `momentum_lt` — low-turnover factor tilt (12-month momentum, inverse-volatility weighted),
  needs only price-derived features.
- `ml_lt` — the trained long-term model (ML-03/ML-04): LightGBM over the current feature set, scored
  as predicted cross-sectional rank centered at 0, same top-15 / 20%-cap discipline as
  `momentum_lt`. Resolves the newest artifact under `ML_LT_MODEL_DIR` (default
  `models/ml_lt/`, written by `ibkr-trader train run`); needs the `[ml]` extra. It refuses
  to score (goes to cash) if the artifact's feature-set version doesn't match the code's,
  and each run pins `model_version` + `feature_set_version` into `backtest_runs.params`.
- `ml_lt_ridge` — the numeric-feature ridge model saved in the same versioned artifact as
  `ml_lt`, with the same centered-rank score and top-15 / 20%-cap allocation. Loading refuses
  artifacts trained by a different scikit-learn major/minor release (retrain required), and it
  shares the feature-set mismatch guard. This is the deployable linear candidate that led the
  ML-05 stitched OOS return comparison; it remains paper/shadow-only evidence, not live trading.
- `ScoreAllocator` — adapter that turns any registered `Predictor` into an allocator, so a
  trained model (or a future sentiment model) plugs straight in without engine changes.

## Simulation realism ([`backtest/engine.py`](../src/ibkr_trader/backtest/engine.py))

- **No look-ahead:** decide at close(t), fill at **open(t+1)**; eligibility/features use bars ≤ t.
- **CAD base currency:** US-priced bars are converted through a `USDCAD` daily series, so FX is
  both a source of return and of risk. Shifting the CAD↔USD mix pays a conversion spread
  (`fx_conversion_bps`), netted per currency so a US→US reshuffle is free.
- **Rebalance discipline:** considered on a cadence, but a name only trades when its weight
  drifts past `rebalance_band` — combined with the churn penalty, turnover stays low.
- **Benchmark:** every run also computes buy-and-hold of the benchmark and reports excess return.
- Runs persist to `backtest_runs` (account, budget, cost model, model_version pinned into
  `params`) so `backtest compare` ranks like-with-like.

## Run it

```bash
# owner policy: decisions start 2010-01-04 (skips the 2008-09 recession); --start is earlier
# so features get warm-up bars instead of a year of forced cash
ibkr-trader backtest run --strategy ml_lt_ridge --account tfsa \
    --universe-file tickers.txt --start 2008-06-02 --eval-start 2010-01-04 --end 2030-01-01
ibkr-trader backtest compare --sort-by calmar        # leaderboard
ibkr-trader report       # static HTML report: leaderboard + equity/drawdown charts, opens in
                         # the browser; no server stays resident (needs the [report] extra:
                         # uv sync --extra report)
```

Every persisted run stores its daily equity curve (and the benchmark's) in
`backtest_runs.metrics` — that is what the report plots; runs persisted before curve
storage list but don't chart. VS Code tasks cover all of this: `Backtest: Run` (the ETF floor
is its default universe), `Backtest: Compare (Leaderboard)`, `Backtest: OOS (Honest Per-Fold)`,
`Train: Run`, `Report: Generate (Static HTML)`.

**Survivorship bounds:** `tickers.txt` results are upper bounds (curated from today's
survivors). `tickers-etfs.txt` — 24 broad-market ETFs (XIU/XIC/XBB/XSP listed 1999-2002,
SPY 1993, plus the CAD core suite and US-listed SPY/VTI/VOO/QQQ/IWM/EFA/EEM/AGG) — is the
essentially bias-free **lower** bound: broad ETFs don't get delisted the way stocks do, so
quote both bounds and the truth lies between. Keep that file broad-market only. The real fix
(point-in-time index membership + a delisted-data provider) remains an open TODO item.

Prices come from the DB (ingest first, e.g. `ibkr-trader ingest prices XEQT --source fmp`); the
benchmark and USD names need their bars ingested too. For US names, also ingest the FX series so
holdings can be valued in CAD:

```bash
ibkr-trader ingest fx --pair USDCAD                  # FMP: ~5y of daily CAD-per-USD
ibkr-trader ingest fx --pair USDCAD --source yahoo   # Yahoo: deep history (2003-09-17+)
```

USDCAD coverage is 2003-09-17→present (Yahoo deep backfill; `serve`'s FX poll keeps both
provider series current and the loader picks one source per window by widest coverage).
Prefer `ADJUSTED_LAST` daily bars for return accuracy (the loader falls back to `TRADES` if
adjusted bars are absent).

## Next steps

- Fundamental solvency screen (market cap, Altman-Z / distress) once fundamentals are ingested.
- Precise per-security dividend cashflows (replaces the yield-based withholding approximation).
- Asset-location optimization: steer US-dividend assets to RRSP/LIRA, growth to TFSA/FHSA.
- Sentiment/news features feeding a trained `Allocator` via `ScoreAllocator`.
