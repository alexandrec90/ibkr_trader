# IBKR Market Data & Historical Data

> Researched 2026-07-04. Primary sources:
> <https://interactivebrokers.github.io/tws-api/historical_bars.html> and
> <https://interactivebrokers.github.io/tws-api/historical_limitations.html>

## Historical bars — `reqHistoricalData`

Parameters (in `ib_async`: `IB.reqHistoricalData(contract, endDateTime, durationStr,
barSizeSetting, whatToShow, useRTH, formatDate, keepUpToDate)`):

- `endDateTime` — end of the window; empty string = "now".
- `durationStr` — how far back: units `S` (sec), `D`, `W`, `M`, `Y` (e.g. `"1 Y"`, `"30 D"`).
- `barSizeSetting` — `1 secs` … `1 min`, `5 mins`, `1 hour`, `1 day`, `1 week`, `1 month`.
- `whatToShow` — `TRADES`, `MIDPOINT`, `BID`, `ASK`, `BID_ASK`, `ADJUSTED_LAST`
  (split/dividend-adjusted trades), `HISTORICAL_VOLATILITY`, `OPTION_IMPLIED_VOLATILITY`,
  `SCHEDULE`, … (availability depends on instrument type).
- `useRTH` — 1 = regular trading hours only.
- `keepUpToDate=True` — stream bar updates after the snapshot (needs bar size ≥ 5 s and empty
  `endDateTime`).

TRADES volume is filtered (block trades/combos executed away from NBBO are excluded), so
volumes won't exactly match other vendors.

## Pacing limitations (hard rules — the ingestion layer must throttle)

A pacing violation occurs when any of these are exceeded:

- Identical historical request within **15 seconds**.
- **6+** historical requests for the same Contract+Exchange+TickType within **2 seconds**.
- More than **60 historical requests within any 10-minute window**.
- `BID_ASK` requests count **double**.
- Max **50 simultaneous open** historical requests.

Additional availability constraints:

- Bars **≤ 30 seconds** are only available for the trailing **6 months**.
- Daily+ bars go back years (decades for liquid stocks); pacing for bars ≥ 1 min is officially
  "relaxed" but soft-throttled.
- No historical data for expired options/warrants/structured products or delisted tickers
  (**survivorship bias risk for backtests** — consider a secondary EOD source such as the
  fundamental/market-data APIs in `docs/reference/data-sources.md`).
- Expired futures: only ~2 years past expiry.

Practical budget: ~1 request/10 s sustained keeps you safely under all limits; the skeleton's
`ibkr_historical` connector enforces a conservative token bucket.

## Streaming / delayed market data

- Live streaming (`reqMktData`, `reqTickByTickData`, depth) requires **paid market data
  subscriptions per exchange, per username** (Client Portal → Settings → Market Data
  Subscriptions). US consolidated equities bundles are cheap for non-professionals;
  TSX (Canadian) data is a separate subscription. **[verify pricing]**
- Without a subscription you can request **delayed data** (15–20 min):
  `reqMarketDataType(3)` then `reqMktData(...)` returns delayed ticks (type 3 = delayed,
  4 = delayed-frozen). Delayed data: Level 1 + historical only — no tick-by-tick, no depth.
- The number of simultaneous live market data lines is limited (base ~100, scales with
  commissions/equity). **[verify]**

## What this means for the pipeline

- **Bulk backfill** of daily bars via IBKR is feasible (one request per symbol per ~duration
  chunk) but slow for large universes → use Alpha Vantage/FMP/Finnhub for wide EOD coverage,
  and IBKR historical for the instruments actually traded (highest fidelity, matches broker).
- Intraday (1-min) backfill: only ~6 months for ≤30 s bars; 1-min bars go further back
  **[verify how far]**; plan to *accumulate* intraday data continuously into Postgres rather
  than assume it can be re-downloaded later.
- Always store `whatToShow`, bar size and source alongside bars (schema does this) so adjusted
  vs. unadjusted series never get mixed.

## Sources

- <https://interactivebrokers.github.io/tws-api/historical_bars.html>
- <https://interactivebrokers.github.io/tws-api/historical_limitations.html>
- <https://interactivebrokers.github.io/tws-api/market_data.html>
- <https://www.interactivebrokers.com/en/trading/papertrader-delayed-data.php>
