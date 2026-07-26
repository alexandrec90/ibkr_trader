# TWS API — Connectivity & Setup

> Researched 2026-07-04. Primary source: https://interactivebrokers.github.io/tws-api/initial_setup.html

## Requirements

- A running, logged-in **TWS** or **IB Gateway** instance — the API is a socket interface *to
  that program*, not to IBKR's servers directly.
- TWS build 952.x+; Python 3 (the project uses `ib_async` on top of the official `ibapi` protocol).

## Enabling the API

- **TWS**: `Edit → Global Configuration → API → Settings` → check *Enable ActiveX and Socket
  Clients*. **Not enabled by default.**
- **IB Gateway**: accepts socket connections by default.
- **Read-Only API** is **ON by default** — orders cannot be placed and order info is hidden
  until you uncheck it. For the ingestion-only phases of this project, leaving it read-only is
  a nice safety rail.
- *Trusted IPs*: hosts other than 127.0.0.1 must be added to the trusted IP list in the same
  settings pane, otherwise every connection pops a confirmation dialog. **[verify]** exact UI
  wording — relevant for Docker networking where the app connects from another container's IP.

## Default socket ports

| Program | Live | Paper |
|---|---|---|
| TWS | 7496 | 7497 |
| IB Gateway | 4001 | 4002 |
| gnzsnz Docker image (socat forward) | 4003 | 4004 |

All are configurable. When live and paper run on the same machine, **triple-check which port
your client connects to.**

## Client IDs

- Each connected API client passes a `clientId` (int). Multiple clients can connect to one
  TWS/Gateway simultaneously, each with a **unique** clientId.
- **Master Client ID** (optional, set in API settings): that client automatically receives
  callbacks for *all* open orders and commission reports across all clients. Useful for a
  monitoring process.
- Client ID 0 additionally receives orders placed manually in the TWS UI. **[verify]**

## Rate limit

- TWS accepts at most **~50 messages/second** from all connected API clients combined.
  Exceeding it disconnects/errors. There is no limit on TWS→client traffic.

## Practical implications for this project

- The service needs the IB Gateway container reachable at `IBKR_HOST:IBKR_PORT`
  (env-configured; default paper port).
- Keep one long-lived connection per process; `ib_async` maintains the event loop and
  reconnect logic.
- Budget message rate: batch historical requests and throttle order/market-data calls well
  below 50 msg/s.

## Sources

- https://interactivebrokers.github.io/tws-api/initial_setup.html
- https://interactivebrokers.github.io/tws-api/introduction.html
