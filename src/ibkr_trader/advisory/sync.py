"""Build the planner's inputs from the live Questrade accounts and a strategy's targets."""

from collections import defaultdict
from datetime import datetime

from ibkr_trader.accounts import AccountType
from ibkr_trader.advisory import questrade as qt
from ibkr_trader.advisory.planner import AccountState, Holding, Target
from ibkr_trader.advisory.targets import StrategyTargets, TargetWeight, questrade_candidates

#: Questrade's account-type enumeration -> the tax profile the planner applies. Spousal
#: and locked-in RRSPs share their parent plan's US-withholding treatment; anything else
#: (Margin, Cash) is non-registered.
QT_ACCOUNT_KINDS: dict[str, AccountType] = {
    "TFSA": AccountType.TFSA,
    "RRSP": AccountType.RRSP,
    "SRRSP": AccountType.RRSP,
    "FHSA": AccountType.FHSA,
    "LIRA": AccountType.LIRA,
    "LRRSP": AccountType.LIRA,
}


def account_kind(qt_type: str) -> AccountType:
    return QT_ACCOUNT_KINDS.get(qt_type.upper(), AccountType.NONREG)


def usdcad_from(combined: dict[str, qt.QtBalance]) -> float | None:
    """CAD per USD implied by the account's combined equity, stated in both currencies."""
    cad, usd = combined.get("CAD"), combined.get("USD")
    if cad and usd and cad.total_equity > 0 and usd.total_equity > 0:
        return cad.total_equity / usd.total_equity
    return None


def _currency(symbol: str, meta: dict[int, qt.QtSymbol], symbol_id: int) -> str:
    """The listing currency from Questrade's symbol record; the ``.TO`` suffix only as a
    fallback when the record is missing."""
    if symbol_id in meta and meta[symbol_id].currency:
        return meta[symbol_id].currency.upper()
    return "CAD" if symbol.upper().endswith(".TO") else "USD"


def account_state(
    client: qt.QuestradeClient, account: qt.QtAccount, *, now: datetime | None = None
) -> AccountState:
    per_currency, combined = qt.balances(client, account.number)
    held = qt.positions(client, account.number)
    working = qt.open_orders(client, account.number, now=now)
    meta = qt.symbols_by_id(
        client, sorted({p.symbol_id for p in held} | {o.symbol_id for o in working})
    )
    pending_buys: dict[str, float] = defaultdict(float)
    committed: dict[str, float] = defaultdict(float)
    for order in working:
        if order.side.startswith("Buy") and order.limit_price:
            value = order.open_quantity * order.limit_price
            pending_buys[order.symbol] += value
            committed[_currency(order.symbol, meta, order.symbol_id)] += value
    usdcad = usdcad_from(combined) or 1.0
    cad = combined.get("CAD")
    return AccountState(
        number=account.number,
        kind=account_kind(account.type),
        label=account.type,
        equity_cad=cad.total_equity if cad else 0.0,
        usdcad=usdcad,
        available={
            ccy: max(0.0, min(b.cash - committed[ccy], b.buying_power))
            for ccy, b in per_currency.items()
        },
        holdings=tuple(
            Holding(p.symbol, _currency(p.symbol, meta, p.symbol_id), p.quantity, p.price)
            for p in held
        ),
        pending_buys=dict(pending_buys),
        pending_sells=frozenset(o.symbol for o in working if o.side.startswith("Sell")),
    )


def account_states(client: qt.QuestradeClient) -> list[AccountState]:
    return [account_state(client, a) for a in qt.accounts(client) if a.status == "Active"]


def _resolve(client: qt.QuestradeClient, weight: TargetWeight) -> qt.QtSymbol | None:
    for name in questrade_candidates(weight.symbol, weight.currency):
        found = qt.symbol_by_name(client, name)
        if found and found.tradable and found.currency.upper() == weight.currency.upper():
            return found
    return None


def resolve_targets(
    client: qt.QuestradeClient, targets: StrategyTargets
) -> tuple[list[Target], list[str]]:
    """Map the snapshot's names onto tradable Questrade listings and price them."""
    notes: list[str] = []
    resolved: list[tuple[TargetWeight, qt.QtSymbol]] = []
    for weight in targets.weights:
        symbol = _resolve(client, weight)
        if symbol is None:
            notes.append(f"{weight.symbol}: no tradable {weight.currency} listing on Questrade")
            continue
        resolved.append((weight, symbol))
    live = qt.quotes(client, [s.symbol_id for _, s in resolved])
    out: list[Target] = []
    for weight, symbol in resolved:
        quote = live.get(symbol.symbol_id)
        price = (quote.last if quote else None) or symbol.prev_close
        if price is None:
            notes.append(f"{symbol.symbol}: no quote or previous close; skipped")
            continue
        out.append(
            Target(
                symbol=symbol.symbol,
                currency=symbol.currency.upper(),
                weight=weight.weight,
                price=price,
                tick=symbol.tick_for(price),
                daily_vol=weight.daily_vol,
                broad_etf=weight.broad_etf,
                delayed=quote is None or quote.delayed,
            )
        )
    return out, notes
