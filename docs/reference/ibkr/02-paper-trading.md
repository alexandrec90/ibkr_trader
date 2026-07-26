# IBKR Paper Trading

> Re-verified 2026-07-16 against current IBKR Campus and IBKR Client Portal guides.

## What it is

A simulated trading environment tied to your live account, using (near-)real market conditions.
It exercises the **same API** as live trading — same TWS/Gateway software, same calls — so code
written against paper works against live by changing the login + port.

## Setting it up

1. Have an approved live IBKR account. Quebec residents are clients of
   **Interactive Brokers Canada Inc.** (CIRO member, CIPF covered).
2. New individual clients normally receive a paper account automatically. Older account
   structures may need to activate one in **Client Portal → Settings → Account Configuration
   → Paper Trading Account**.
3. That page shows the separate **Paper Trading Username** and **Paper Trading Account Number**
   and lets you reset its password. The account number starts with **`DU`**.
4. Log into TWS or IB Gateway with the **paper** credentials (TWS login screen also has a
   "paper trading" toggle). API port: TWS 7497 / Gateway 4002.
5. Paper accounts start with USD 1,000,000 of simulated Equity with Loan Value and can be reset
   in Client Portal.

## Multiple funded accounts are not four paper accounts

The owner's non-registered margin, RRSP, TFSA and FHSA account numbers identify funded accounts.
They must not be used as order targets while `ENVIRONMENT=paper`. IBKR documents one paper
account per live account structure; a linked live username can separately manage multiple live
accounts, but that does not make the live account IDs simulated.

Configuration therefore keeps the identities separate:

| Setting | Meaning | Paper execution target? |
|---|---|---|
| `IBKR_PAPER_ACCOUNT` | Separate simulated `DU...` account | Yes |
| `IBKR_MARGIN_ACCOUNT` | Non-registered individual margin account (`nonreg`) | No |
| `IBKR_RRSP_ACCOUNT` | Funded RRSP | No |
| `IBKR_TFSA_ACCOUNT` | Funded TFSA | No |
| `IBKR_FHSA_ACCOUNT` | Funded FHSA | No |

The offline backtester still models RRSP/TFSA/FHSA tax and cost differences using
`AccountType`; the single paper account validates connectivity and order plumbing, not the tax
wrapper. When the API connection is implemented, it must compare the configured `DU...` value
with the account IDs returned by `reqManagedAccts()` before accepting any paper order.

## Market data on paper accounts

Two options:

1. **Free delayed data** (no subscription): 15–20 min delayed, **top-of-book (Level 1) and
   historical data only** — no tick-by-tick, no Level 2 depth. Request it via
   `reqMarketDataType(3)` (delayed) before `reqMktData`, otherwise you get subscription errors.
2. **Share live subscriptions**: in Client Portal → Account Settings → Paper Trading Account →
   *share real-time market data subscriptions with paper account = Yes*. Caveats:
   - Takes **up to 24 h** to take effect.
   - The live and paper usernames **cannot consume the shared data at the same time**.

## Known simulation limitations

Paper fills are simulated against real quotes but the simulator is optimistic/naive compared to
a real order book. Treat paper results as *plumbing validation*, not as evidence of strategy
profitability — that's what the backtester with realistic cost/slippage models is for.

Documented/commonly-reported limitations **[verify each before relying on it]**:

- Fill simulation does not model queue position or market impact.
- Some order types and exotic instruments behave differently or are unsupported.
- Short-sale availability ("shortable shares") data may differ from live.

## How this project uses it

- `ENVIRONMENT=paper` (default) in `.env` → the execution layer connects to the paper port and
  refuses to start against a live port unless `ENVIRONMENT=live` **and**
  `LIVE_TRADING_ACKNOWLEDGED=true` are both set. See `src/ibkr_trader/execution/`.
- Model prototyping loop: backtest offline → run strategy against paper via the same
  `Broker` interface → compare realized vs. backtested behaviour.

## Sources

- https://www.interactivebrokers.com/campus/glossary-terms/paper-trading-account/
- https://www.interactivebrokers.com/campus/trading-lessons/how-to-open-an-ibkr-paper-trading-account/
- https://www.interactivebrokers.com/campus/ibkr-api-page/market-data-subscriptions/
- https://www.ibkrguides.com/clientportal/papertradingaccount.htm
- https://www.ibkrguides.com/clientportal/aboutpapertradingaccounts.htm
- https://www.interactivebrokers.com/en/trading/papertrader-delayed-data.php
- https://interactivebrokers.github.io/tws-api/introduction.html
