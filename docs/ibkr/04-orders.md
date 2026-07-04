# IBKR Orders — Lifecycle & API

> Researched 2026-07-04. Primary source: https://interactivebrokers.github.io/tws-api/order_submission.html

## Order IDs

- On connect, TWS sends `nextValidId` — the first usable order ID. Every order needs an ID
  **greater than any previously used** for that username.
- Single-client apps can just increment locally. Multiple clients placing orders must
  coordinate (or call `reqIds` before each order). `ib_async` manages this automatically.
- `permId` (permanent ID assigned by IBKR) is the stable identifier across sessions — store
  **both** `orderId` and `permId` (the schema's `orders` table does).

## Placing / modifying / cancelling

- `placeOrder(orderId, contract, order)` submits. Re-calling `placeOrder` with the **same
  orderId** and modified parameters modifies the working order. `cancelOrder(orderId)` cancels.
  **[verify]** modify semantics for the exact fields that can change in-flight.
- In `ib_async`: `ib.placeOrder(contract, order)` returns a `Trade` object whose status updates
  live via events; `ib.cancelOrder(order)` cancels.

## Status callbacks

After submission TWS emits:

- `openOrder` — order + `OrderState` (includes **pre-trade margin & commission estimates** —
  useful for risk checks: submit with `whatIf=True` to get the estimate *without* transmitting).
- `orderStatus` — status, filled qty, remaining, avg fill price.
- `execDetails` — per-fill execution reports; `commissionReport` follows each execution.

States: `PendingSubmit → PreSubmitted → Submitted → Filled` / `Cancelled` / `Inactive`.

> ⚠️ "There are not guaranteed to be orderStatus callbacks for every change in order status" —
> reconcile with `execDetails` and periodic `reqOpenOrders`/`reqPositions` instead of trusting
> `orderStatus` alone. The skeleton's `OrderReconciler` stub exists for this.

## Contracts

Orders need a fully-qualified `Contract` (symbol, secType, exchange, currency — e.g.
`Stock('AAPL', 'SMART', 'USD')`). Ambiguous contracts are rejected; resolve first via
`qualifyContracts` / `reqContractDetails` and cache the resulting `conId` (the `instruments`
table stores it).

- `SMART` is IBKR's routing exchange; Canadian listings need `exchange='SMART'` +
  `currency='CAD'` (primary exchange TSE) **[verify TSX naming: `TSE` in IBKR terms]**.

## Safety rails used in this project

- Paper by default; live requires two explicit env flags (see `config.py`).
- `whatIf=True` margin/commission preflight before real submission.
- Per-order and per-day notional caps in `execution/risk.py` (stub — implement before any live use).
- Read-Only API stays enabled in the Gateway until execution work actually starts.

## Sources

- https://interactivebrokers.github.io/tws-api/order_submission.html
- https://ib-api-reloaded.github.io/ib_async/
