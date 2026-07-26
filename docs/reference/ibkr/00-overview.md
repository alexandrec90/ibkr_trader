# IBKR API — Overview

> Researched 2026-07-04 from official IBKR sources. Items marked **[verify]** could not be
> confirmed from primary documentation during research (IBKR Campus blocks automated fetching)
> and should be double-checked by hand at https://www.interactivebrokers.com/campus/ibkr-api-page/ibkr-api-home/

## API offerings

IBKR exposes several programmatic interfaces:

| API | Transport | Requires | Best for |
|---|---|---|---|
| **TWS API** | TCP socket to a running TWS or IB Gateway instance | TWS or IB Gateway running & logged in | Full-featured algo trading, streaming market data, historical data |
| **Web API (Client Portal API / CPAPI)** | REST over HTTPS (+ websocket) | Client Portal Gateway (Java) or OAuth | Lighter REST integrations, account mgmt |
| **FIX** | FIX protocol | Dedicated FIX connection (institutional) | Institutions, high volume |
| **Excel API** | RTD/ActiveX | Windows + TWS | Spreadsheet workflows |

Notes:

- IBKR is consolidating its web products (Client Portal Web API, Digital Account Management,
  Flex Web Service) into a single **IBKR Web API** with OAuth 2.0. Existing endpoints are not
  deprecated. ([source](https://www.interactivebrokers.com/en/trading/ib-api.php))
- An IB username can only have **one brokerage (trading-enabled) session at a time**. Logging
  into TWS with the same username kicks out the Gateway session (and vice versa). Plan separate
  usernames or use the paper account for the bot.
- The classic docs at https://interactivebrokers.github.io/tws-api/ (v9.72+) are still accurate
  for fundamentals but are superseded by IBKR Campus docs, which must be browsed manually
  (automated fetching returns HTTP 403).

## Decision for this project

**TWS API via IB Gateway (headless, Dockerized) + the `ib_async` Python library.**

Reasons:

- TWS API is the most complete API (orders, streaming + historical data, account data).
- IB Gateway is a lighter, headless-friendly alternative to full TWS; community Docker images
  (see [05-running-in-docker.md](05-running-in-docker.md)) handle the login automation.
- `ib_async` (https://github.com/ib-api-reloaded/ib_async) is the actively maintained fork of
  the well-known `ib_insync` library (original author passed away in early 2024; the fork is
  the successor — do **not** depend on `ib-insync`, it is unmaintained). It wraps the raw
  socket API in a sane sync/async Python interface.
- The Client Portal API remains an option for account-management endpoints later.

## Hard limits to design around

- **Max ~50 messages/second** from all API clients combined into one TWS/Gateway instance.
- Historical data **pacing rules** — see [03-market-data-and-historical.md](03-market-data-and-historical.md).
- Market data subscriptions cost money and are **per-username**; paper accounts can share the
  live account's subscriptions — see [02-paper-trading.md](02-paper-trading.md).
- IB Gateway sessions require **weekly manual 2FA re-authentication** (tokens invalidated
  Sunday ~1:00 am ET) — a truly unattended cloud deployment needs a plan for this
  (see [05-running-in-docker.md](05-running-in-docker.md)).

## Doc map (this folder)

- [01-connectivity-and-setup.md](01-connectivity-and-setup.md) — ports, API settings, client IDs
- [02-paper-trading.md](02-paper-trading.md) — paper account setup, market data on paper
- [03-market-data-and-historical.md](03-market-data-and-historical.md) — historical bars, pacing, delayed data
- [04-orders.md](04-orders.md) — order lifecycle, IDs, callbacks
- [05-running-in-docker.md](05-running-in-docker.md) — headless IB Gateway in Docker, 2FA, restarts
- [06-products-and-contracts.md](06-products-and-contracts.md) — product families, contract types, repo implications

## Sources

- https://www.interactivebrokers.com/en/trading/ib-api.php
- https://interactivebrokers.github.io/tws-api/introduction.html
- https://interactivebrokers.github.io/cpwebapi/
- https://github.com/ib-api-reloaded/ib_async
