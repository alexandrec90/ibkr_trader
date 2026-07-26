# IBKR Products & Contract Coverage

> Researched 2026-07-04 from official IBKR documentation. This is an engineering map for
> paper-trading support, not a recommendation to trade any product.

IBKR's APIs are generally **contract-based**: market data and orders are requested against a
fully specified financial instrument contract. In TWS API terms the core discriminator is
`Contract.secType`; IBKR Campus recommends resolving/caching `conId` plus exchange where
possible to avoid ambiguous symbol definitions.

## Product map

| Product family | Programmatic support | Typical TWS `secType` / notes |
|---|---:|---|
| Stocks and ETFs | Yes | `STK`; leveraged/inverse ETPs are usually exchange-traded products under stock/ETF-style contracts, subject to product permissions and warnings. |
| Bonds / fixed income | Yes | `BOND`; contract discovery/details are more specialized than stocks. |
| Warrants | Yes | `WAR`. |
| Equity options | Yes | `OPT`; requires expiry, strike, right, exchange, multiplier/local symbol, or resolved `conId`. |
| Futures | Yes | `FUT`; regular futures can trade. `CONTFUT` continuous futures are historical-data-only and cannot be used for real-time data or orders. |
| Futures options | Yes | `FOP`. |
| Single-stock futures / security futures | Usually via futures/security-futures contracts where IBKR offers them | Treat as a futures-like product with additional eligibility/risk disclosure checks. Verify exchange availability and exact contract definition in TWS/IBKR docs before implementation. |
| Currency / forex | Yes | `CASH`; pair-style contracts such as base currency + quote currency. |
| Complex / combo products | Yes for API-supported combos | `BAG` combo contracts for multi-leg orders. Each leg still needs a qualified underlying contract. |
| Prediction markets / event contracts | Yes, but special workflow | IBKR models event contracts as option-like products: ForecastEx products as options and CME event contracts as futures options. Eligibility depends on account/entity. |

## Engineering implications for this repo

- The current implementation is stock-first: FMP ingestion maps ticker symbols to
  `Instrument(symbol, exchange, currency)`, IBKR stubs show `Stock(...)`, and `OrderRequest`
  carries only `symbol`, side, quantity, and simple order fields.
- The existing `instruments.sec_type` column is the right start, but derivatives and fixed
  income need more contract identity fields or a normalized `contract_details` table: `conId`,
  expiry, strike, right, multiplier, local symbol/trading class, primary exchange, legs, and
  product-specific metadata.
- Market data, features, backtests, predictions, orders, executions, and risk checks can stay
  structurally reusable because they already key off `instrument_id`; product-specific logic
  should live in contract resolution, data normalization, strategy/risk sizing, and execution.
- Any expansion must stay paper-only unless the owner separately changes policy; every order
  path must keep `Settings.assert_trading_allowed()` and `RiskChecker.check()`.

## Sources

- https://interactivebrokers.github.io/tws-api/classIBApi_1_1Contract.html
- https://interactivebrokers.github.io/tws-api/basic_contracts.html
- https://www.interactivebrokers.com/campus/ibkr-api-page/contracts/
- https://www.interactivebrokers.com/campus/ibkr-api-page/order-types/
- https://www.interactivebrokers.com/campus/ibkr-api-page/event-contracts/
- https://www.interactivebrokers.com/campus/ibkr-api-page/event-trading/
