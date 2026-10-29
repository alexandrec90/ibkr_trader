# Paper trading

The paper loop trades one registered strategy (`PAPER_STRATEGY`, default `momentum_lt`) on
the IBKR **paper** account (`IBKR_PAPER_ACCOUNT`, `DU…`). It is plumbing validation plus
forward evidence, not proof of profitability: the paper simulator fills naively
([02-paper-trading.md](../reference/ibkr/02-paper-trading.md)). Live trading stays off;
nothing here changes `ENVIRONMENT=paper` or the `assert_trading_allowed()` gate.

## What it does

Once per calendar month, inside 10:00–15:30 New York time on a weekday:

1. **Sync.** Copies order statuses and fills from the gateway into `orders` and
   `executions` (`execution/reconcile.py`). While any earlier order is still working,
   nothing new is sent.
2. **Decide.** Computes the strategy's target weights exactly as the forward-shadow
   snapshot does (`backtest.snapshot.decision_inputs`), restricted to **US listings**.
   It loads only the last `DECISION_LOOKBACK_DAYS` (550) of bars: on the 34 GB live
   database that peaks at about 316 MB, and takes about 100 s, against several GB for all
   history. It refuses when the newest daily bar is older than `PAPER_MAX_BAR_AGE_DAYS`.
   An instrument whose bars fail validation (a provider's placeholder row) is left out
   and listed in the month's `invalid_series`; it never aborts the decision.
3. **Size.** The sleeve is `PAPER_CAPITAL_CAD`, plus what the strategy's fills raised and
   minus what they spent. Each fill is converted at the USDCAD rate of its day. The rest
   of the ~1M paper account is never touched.
4. **Trade.** Uses the backtest engine's rule: a name trades only past the 5% rebalance
   band, sells go before buys, and quantities are whole shares rounded toward zero. Each
   order is a DAY market order through `IbkrBroker.place_order`, which runs:
   `assert_trading_allowed()` → paper-account resolution → **`RiskChecker.check()`**
   (halt switch, USD only, quote within 15% of the planned close, regular hours,
   per-order, per-day and per-position caps, no short sales) → whatIf preflight →
   transmit → an `orders` row.
5. **Record.** The month's decision becomes a `strategy_snapshots` row named
   `paper:<strategy>`, with the orders sent and any refused. `snapshot report` scores it
   alongside the forward-shadow rows.

A refused order does not stop the others. The month is still recorded, and the job fails,
so `ibkr-trader health` goes red instead of retrying into the same refusal every 15
minutes.

### Why US listings only

IBKR Canada does not allow its Canadian-resident clients to submit orders through their
own API application for products traded on a Canadian marketplace. This follows CIRO
requirements ([TWS API limitations](https://www.interactivebrokers.com/docs/tws-api/doc/notes-limitations/limitations/canadian-residents-restricted-from-programmatically-trading-canadian-products)).
A TSX strategy could therefore never go live through this app, and IBKR may reject such
orders on paper too. The planner drops Canadian listings before allocating, and
`RiskChecker` refuses any non-USD order as a second line. Canada exposure would come from
US-listed funds.

## Turning it on

1. `.env`: set `READ_ONLY_API=no`, then recreate the gateway so it picks the change up:
   `docker compose --profile ibkr up -d --force-recreate ib-gateway`. Approve the IBKR
   Mobile login if one arrives.
2. Check the connection: `ibkr-trader ibkr-check` should print the `DU…` account and a
   delayed SPY quote.
3. Make sure prices are fresh: the `app` container's ingestion must be running
   (`docker compose --profile app up -d --build app`).
4. Dry run: `ibkr-trader paper plan`. It is read-only and prints the trades a rebalance
   would send now.
5. Either trade once by hand during market hours with `ibkr-trader paper rebalance`, or
   let `serve` do it: `PAPER_TRADING_ENABLED=true` (and `GATEWAY_WATCH_ENABLED=true`, so
   a logged-out gateway reaches your phone), then rebuild the `app` container.

## Two PCs

The collector (`app` + `db`) runs on one PC, and modeling and decisions happen on another.
There is still **one** database: the collector's. A copy on the research PC would drift,
and every `backtest_runs`, snapshot or order row written there would be lost on the next
refresh.

| On | What runs | How |
| --- | --- | --- |
| Collector PC | `app`, `db`, `ib-gateway`, the `paper` job | `db` stays published on `127.0.0.1:5433`; `tailscale serve --bg --tcp 5433 tcp://127.0.0.1:5433` forwards **tailnet-only** TCP to it. Never `tailscale funnel`. |
| Research PC | `ibkr-trader` CLI, training, backtests, `paper plan` | `.env` `DATABASE_URL=postgresql+psycopg://trader:<password>@<collector>.<tailnet>.ts.net:5433/ibkr_trader`; no local `db` container. |

- **Password.** The forward makes the database reachable from every tailnet device, so
  it must not keep the compose default `trader`. Rotating it takes four steps: run
  `ALTER ROLE trader PASSWORD '…'` (via `docker exec -i … psql`, with the value on
  stdin), set `POSTGRES_PASSWORD` in `.env` along with the password inside its
  `DATABASE_URL`, then `docker compose --profile app up -d db app`, since the app's URL
  is built from `POSTGRES_PASSWORD`.
- **Memory is spent where the database is.** Heavy reads from the research PC are served
  by the collector's `db` container, which is capped by `DB_MEM_LIMIT`. Decisions load
  only `paper.DECISION_LOOKBACK_DAYS` of bars. The forward-shadow `snapshot run` still
  loads all history, so run it with the collector PC's memory in mind.
- **One gateway session.** IBKR allows one session per username, so only the collector
  PC's `ib-gateway` logs in. On the research PC, `paper plan` and `ibkr-check` need that
  gateway, so run them on the collector PC, or skip the gateway and use `snapshot` and
  `backtest` from the research PC.
- **Remove it:** `tailscale serve --tcp=5433 off` on the collector PC.

## Day to day

| Command | What it does |
| --- | --- |
| `ibkr-trader paper plan` | Shows the trades a rebalance would send now (read-only) |
| `ibkr-trader paper rebalance [--force]` | Syncs, then rebalances if the month is not done (`--force`: even if it is) |
| `ibkr-trader paper sync` | Records statuses and fills, then lists working orders |
| `ibkr-trader paper cancel <orders.id>` | Asks IBKR to cancel one working order |
| `ibkr-trader paper halt` / `resume` | Creates or removes the halt file |
| `ibkr-trader snapshot report` | Scores the `paper:<strategy>` decisions against XEQT |

The `serve` job `paper` runs every `PAPER_CHECK_MINUTES` but connects only between 09:30
and 16:30 New York time on weekdays. Outside those hours it records
`skipped: outside US market hours`, so the gateway's nightly restart never reads as a
failure.

### Emergency stop

Any one of these refuses every order:

- `ibkr-trader paper halt` on the host. It writes `TRADING_HALT_FILE`, by default
  `logs/TRADING_HALT`.
- Inside the app container, whose `logs/` is the `applogs` volume:
  `docker exec ibkr_trader-app-1 touch /app/logs/TRADING_HALT`.
- `TRADING_HALTED=true` in `.env`, then restart the container.
- `docker compose stop app`, which stops scheduled trading altogether.

Working orders are not cancelled by a halt. Use `paper cancel`.

## Known limits

- **Holidays are not modelled.** A market order sent on an exchange holiday waits for
  the next session. The rebalance window keeps orders inside a weekday.
- **Executions are reported for recent days only.** A fill missed while nothing synced
  for about a week keeps its order's status and average price, but has no `executions`
  row, so the sleeve's cash misses it.
- **Buffered allocators forget their incumbents.** `recent_momentum` and
  `steady_compounders` keep their rank buffer in memory, and each monthly decision builds
  a fresh one. `momentum_lt` has no such state.
- **The sleeve is not segregated at IBKR.** All positions in the paper account are
  treated as the strategy's. A position matching no stored instrument is reported and
  left alone.
