# IBKR Paper Trading

> Researched 2026-07-04. IBKR Campus pages block automated fetching; details below are from
> IBKR's public pages and search summaries — spot-check at
> https://www.interactivebrokers.com/campus/trading-lessons/request-paper-trading-account/

## What it is

A simulated trading environment tied to your live account, using (near-)real market conditions.
It exercises the **same API** as live trading — same TWS/Gateway software, same calls — so code
written against paper works against live by changing the login + port.

## Setting it up

1. Have an approved (funded) live IBKR account. Quebec residents are clients of
   **Interactive Brokers Canada Inc.** (CIRO member, CIPF covered).
2. In **Client Portal → Account Settings → Paper Trading Account**, request the paper account.
   You receive a separate paper username (paper account IDs start with **`DU`**) and password.
3. Log into TWS or IB Gateway with the **paper** credentials (TWS login screen also has a
   "paper trading" toggle). API port: TWS 7497 / Gateway 4002.
4. Paper account equity is simulated (typically starts at USD 1,000,000 and can be reset in
   Client Portal). **[verify]** current default amount.

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

- https://www.interactivebrokers.com/campus/trading-course/ibkr-paper-trading-account/
- https://www.interactivebrokers.com/en/trading/papertrader-delayed-data.php
- https://interactivebrokers.github.io/tws-api/introduction.html
