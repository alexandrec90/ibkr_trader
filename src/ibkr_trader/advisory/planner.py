"""Turn account state plus strategy targets into limit-order tickets. Pure: no I/O.

The rules, each one the owner asked for:

- **Never more than the account can pay for.** Buys are funded per currency from cash
  net of open buy orders, never from sale proceeds that have not settled and never by an
  implied CAD/USD conversion. A target the account cannot fund is a note, not an order.
- **Favour what is already held.** Incumbents are funded first, and a held name already
  near its target is left alone rather than topped up by a few shares.
- **Few large orders.** No order below ``min_order_cad``; a small account concentrates
  into fewer names (``equity / min_order_cad`` at most) instead of spreading thin, and at
  most ``max_new_names`` new names are opened per run.
- **Keep single names within a limit.** A stock's weight is capped at
  ``max_position_pct`` (broad ETFs are exempt: XEQT *is* a diversified book), and a
  holding that drifts past the cap by ``trim_band`` gets a trim.
- **Limit orders, laddered.** Each trade is split into rungs priced ``rung_sigmas`` daily
  standard deviations away from the reference price -- below it to buy, above it to sell
  -- so nothing assumes an instant fill at the market.
"""

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from ibkr_trader.accounts import AccountType, get_profile


@dataclass(frozen=True)
class PlanConfig:
    min_order_cad: float = 1000.0
    max_position_pct: float = 0.20
    max_new_names: int = 3
    trim_band: float = 0.05
    rung_sigmas: tuple[float, ...] = (0.25, 0.75, 1.5)
    rung_weights: tuple[float, ...] = (0.4, 0.35, 0.25)
    min_rung_cad: float = 500.0
    default_daily_vol: float = 0.015
    good_days: int = 7
    #: Sell holdings that are outside the strategy. Off by default: selling a core ETF the
    #: owner chose to fund a strategy's near-equivalent is exactly the churn to avoid.
    exits: bool = False


@dataclass(frozen=True)
class Holding:
    symbol: str
    currency: str
    quantity: float
    price: float

    @property
    def value(self) -> float:
        return self.quantity * self.price


@dataclass(frozen=True)
class AccountState:
    number: str
    kind: AccountType
    label: str
    equity_cad: float
    usdcad: float
    #: Spendable cash per currency, already net of open buy orders.
    available: dict[str, float]
    holdings: tuple[Holding, ...] = ()
    #: Open buy orders per symbol, in the listing currency: counted as if already held so
    #: a re-run does not plan the same buy twice.
    pending_buys: dict[str, float] = field(default_factory=dict)
    #: Symbols with an open sell order: never trimmed or exited a second time.
    pending_sells: frozenset[str] = frozenset()

    @property
    def masked(self) -> str:
        return f"{self.label} ...{self.number[-3:]}"

    def to_cad(self, amount: float, currency: str) -> float:
        return amount * self.usdcad if currency.upper() == "USD" else amount

    def from_cad(self, amount: float, currency: str) -> float:
        return amount / self.usdcad if currency.upper() == "USD" else amount

    def owned(self) -> set[str]:
        return {h.symbol for h in self.holdings} | set(self.pending_buys)

    def held_value(self, symbol: str) -> float:
        held = sum(h.value for h in self.holdings if h.symbol == symbol)
        return held + self.pending_buys.get(symbol, 0.0)


@dataclass(frozen=True)
class Target:
    symbol: str
    currency: str
    weight: float
    price: float
    tick: float = 0.01
    daily_vol: float | None = None
    broad_etf: bool = False
    delayed: bool = False


@dataclass(frozen=True)
class Pricing:
    """Where a ladder starts and how far apart its rungs sit."""

    ref: float
    vol: float
    tick: float


@dataclass(frozen=True)
class OrderTicket:
    account: str
    side: str
    symbol: str
    quantity: int
    limit_price: float
    currency: str
    good_until: date
    note: str

    @property
    def notional(self) -> float:
        return self.quantity * self.limit_price


@dataclass
class AccountPlan:
    account: AccountState
    orders: list[OrderTicket]
    notes: list[str]
    cash_left: dict[str, float]


@dataclass(frozen=True)
class _Run:
    """What every helper of one account's plan needs: kept together to keep signatures short."""

    state: AccountState
    config: PlanConfig
    good_until: date

    def min_rung(self, currency: str) -> float:
        return self.state.from_cad(self.config.min_rung_cad, currency)

    def big_enough(self, notional: float, currency: str) -> bool:
        return self.state.to_cad(notional, currency) >= self.config.min_order_cad


def round_to_tick(price: float, tick: float, side: str) -> float:
    """Snap to the tick grid on the side that never worsens the limit: down to buy, up to sell."""
    steps = price / tick
    snapped = math.floor(steps + 1e-9) if side == "Buy" else math.ceil(steps - 1e-9)
    return round(snapped * tick, 6)


def _split(quantity: int, weights: tuple[float, ...]) -> list[int]:
    total = sum(weights)
    shares = [math.floor(quantity * w / total) for w in weights]
    shares[0] += quantity - sum(shares)
    return shares


def ladder(
    side: str, quantity: int, pricing: Pricing, min_rung_value: float, config: PlanConfig
) -> list[tuple[int, float]]:
    """``(shares, limit)`` rungs, closest to the market first.

    As many rungs as ``rung_sigmas`` allows while every rung -- after rounding to whole
    shares -- is still worth ``min_rung_value`` (listing currency); one rung otherwise.
    """
    direction = -1.0 if side == "Buy" else 1.0
    prices = [
        round_to_tick(pricing.ref * (1 + direction * sigma * pricing.vol), pricing.tick, side)
        for sigma in config.rung_sigmas
    ]
    for count in range(min(len(prices), quantity), 0, -1):
        rungs = list(zip(_split(quantity, config.rung_weights[:count]), prices, strict=False))
        if count == 1 or all(q > 0 and q * p >= min_rung_value for q, p in rungs):
            return rungs
    return []


def cap_weights(weights: dict[str, float], capped: set[str], cap: float) -> dict[str, float]:
    """Clamp ``capped`` names to ``cap`` and hand the excess to the rest, pro rata.

    What cannot be placed anywhere (every name at its cap) stays as cash.
    """
    out = dict(weights)
    for _ in range(len(out) + 1):
        over = {s for s in capped if out[s] > cap + 1e-12}
        if not over:
            break
        excess = sum(out[s] - cap for s in over)
        for s in over:
            out[s] = cap
        room = {s: w for s, w in out.items() if not (s in capped and w >= cap - 1e-12)}
        base = sum(room.values())
        if base <= 0:
            break
        for s, w in room.items():
            out[s] = w + excess * w / base
    return out


def _currency_rank(kind: AccountType, currency: str) -> int:
    """TFSA/FHSA lose 15% of US dividends for good, so they prefer CAD listings."""
    if get_profile(kind).us_dividend_withholding > 0:
        return 0 if currency.upper() == "CAD" else 1
    return 0


def _fundable(state: AccountState, currency: str, config: PlanConfig) -> bool:
    return state.to_cad(state.available.get(currency, 0.0), currency) >= config.min_order_cad


def _funding_order(state: AccountState, targets: list[Target]) -> list[Target]:
    """Incumbents first, then heaviest; ties go to the account's preferred currency."""
    owned = state.owned()
    return sorted(
        targets,
        key=lambda t: (
            t.symbol not in owned,
            -t.weight,
            _currency_rank(state.kind, t.currency),
            t.symbol,
        ),
    )


def _pick(
    state: AccountState, ordered: list[Target], config: PlanConfig
) -> tuple[list[Target], list[Target]]:
    """Walk the funding order: at most ``equity / min_order`` names and ``max_new_names``
    new ones; a new name with no cash in its currency is set aside, not counted."""
    owned = state.owned()
    max_names = max(1, math.floor(state.equity_cad / config.min_order_cad))
    picked: list[Target] = []
    unfunded: list[Target] = []
    new_count = 0
    for target in ordered:
        new = target.symbol not in owned
        if new and not _fundable(state, target.currency, config):
            unfunded.append(target)
            continue
        if len(picked) >= max_names or (new and new_count >= config.max_new_names):
            continue
        picked.append(target)
        new_count += new
    return picked, unfunded


def select_targets(
    state: AccountState, targets: list[Target], config: PlanConfig
) -> tuple[list[tuple[Target, float]], list[Target]]:
    """``(picked, unfunded)``: the names this account aims for, in funding order with weights
    re-spread over them, and the new names it has no cash in their currency to open.

    An unfundable name does not take one of the run's ``max_new_names`` slots, so a TFSA
    holding only USD opens its next USD name instead of wasting a slot on a CAD one.
    """
    targets = [t for t in targets if t.weight > 0]
    picked, unfunded = _pick(state, _funding_order(state, targets), config)
    if not picked:
        return [], unfunded
    scale = sum(t.weight for t in targets) / sum(t.weight for t in picked)
    weights = cap_weights(
        {t.symbol: t.weight * scale for t in picked},
        {t.symbol for t in picked if not t.broad_etf},
        config.max_position_pct,
    )
    return [(t, weights[t.symbol]) for t in picked], unfunded


def _desired(state: AccountState, target: Target, weight: float) -> float:
    """How much more of ``target`` reaches ``weight``, in its listing currency."""
    held_cad = state.to_cad(state.held_value(target.symbol), target.currency)
    return state.from_cad(weight * state.equity_cad - held_cad, target.currency)


def _share_pool(run: _Run, group: list[Target], desired: dict[str, float], pool: float) -> dict:
    """Split one currency's ``pool`` across new names in proportion to their gaps, dropping
    the smallest gap until every share clears the minimum order."""
    group = list(group)
    while group:
        gap = sum(desired[t.symbol] for t in group)
        share = {t.symbol: min(desired[t.symbol], pool * desired[t.symbol] / gap) for t in group}
        small = [t for t in group if not run.big_enough(share[t.symbol], t.currency)]
        if not small:
            return share
        group.remove(min(small, key=lambda t: desired[t.symbol]))
    return {}


def _budgets(
    run: _Run, picked: list[tuple[Target, float]], available: dict[str, float]
) -> dict[str, float]:
    """Cash per picked name: incumbents first, in order, then new names pro rata to their gap
    -- so the heaviest new target cannot swallow the account and leave the rest a remnant."""
    state = run.state
    desired = {t.symbol: _desired(state, t, w) for t, w in picked}
    wanted = [t for t, _ in picked if run.big_enough(desired[t.symbol], t.currency)]
    pool = dict(available)
    budgets: dict[str, float] = {}
    for target in (t for t in wanted if t.symbol in state.owned()):
        budgets[target.symbol] = min(desired[target.symbol], pool.get(target.currency, 0.0))
        pool[target.currency] = pool.get(target.currency, 0.0) - budgets[target.symbol]
    new = [t for t in wanted if t.symbol not in state.owned()]
    for currency in sorted({t.currency for t in new}):
        group = [t for t in new if t.currency == currency]
        budgets.update(_share_pool(run, group, desired, pool.get(currency, 0.0)))
    return budgets


def _tickets(
    run: _Run, side: str, symbol: str, currency: str, rungs: list[tuple[int, float]], why: str
) -> list[OrderTicket]:
    return [
        OrderTicket(
            account=run.state.masked,
            side=side,
            symbol=symbol,
            quantity=qty,
            limit_price=price,
            currency=currency,
            good_until=run.good_until,
            note=f"{why} · rung {i}/{len(rungs)}",
        )
        for i, (qty, price) in enumerate(rungs, start=1)
    ]


def _plan_buy(
    run: _Run, target: Target, weight: float, budget: float, available: dict[str, float]
) -> tuple[list[OrderTicket], str | None]:
    state, config = run.state, run.config
    desired = _desired(state, target, weight)
    if not run.big_enough(desired, target.currency):
        return [], None
    quantity = math.floor(min(budget, available.get(target.currency, 0.0)) / target.price)
    if not run.big_enough(quantity * target.price, target.currency):
        cash = state.available.get(target.currency, 0.0)
        return [], (
            f"{target.symbol}: wants {target.currency} {desired:,.0f} to reach {weight:.0%}; "
            f"not funded this run ({target.currency} {cash:,.0f} spendable)"
        )
    pricing = Pricing(target.price, target.daily_vol or config.default_daily_vol, target.tick)
    rungs = ladder("Buy", quantity, pricing, run.min_rung(target.currency), config)
    available[target.currency] -= sum(q * p for q, p in rungs)
    why = f"{'top up' if target.symbol in state.owned() else 'new'} toward {weight:.0%}"
    if target.delayed:
        why += " · DELAYED quote, check the live price"
    return _tickets(run, "Buy", target.symbol, target.currency, rungs, why), None


def _plan_sell(
    run: _Run, holding: Holding, quantity: int, target: Target | None, why: str
) -> list[OrderTicket]:
    if quantity <= 0 or not run.big_enough(quantity * holding.price, holding.currency):
        return []
    pricing = Pricing(
        holding.price,
        (target.daily_vol if target else None) or run.config.default_daily_vol,
        target.tick if target else 0.01,
    )
    rungs = ladder("Sell", quantity, pricing, run.min_rung(holding.currency), run.config)
    return _tickets(run, "Sell", holding.symbol, holding.currency, rungs, why)


def _weight(state: AccountState, holding: Holding) -> float:
    return state.to_cad(holding.value, holding.currency) / state.equity_cad


def _outside_note(state: AccountState, outside: list[Holding]) -> str:
    """One line for every holding the strategy does not own, heaviest first."""
    ranked = sorted(outside, key=lambda h: -_weight(state, h))
    total = sum(_weight(state, h) for h in outside)
    names = ", ".join(f"{h.symbol} {_weight(state, h):.0%}" for h in ranked)
    return f"{len(outside)} holding(s) outside the strategy ({total:.0%}), left alone: {names}"


def _plan_holding(
    run: _Run, holding: Holding, target: Target | None
) -> tuple[list[OrderTicket], str | None]:
    """Trim (or, with ``exits``, sell) one holding; ``target`` None means outside the strategy."""
    state, config = run.state, run.config
    weight = _weight(state, holding)
    if holding.symbol in state.pending_sells:
        return [], f"{holding.symbol}: open sell order already working; left alone"
    if target is None:
        return _plan_sell(run, holding, math.floor(holding.quantity), None, "exit"), None
    cap = config.max_position_pct
    if target.broad_etf or weight <= cap + config.trim_band:
        return [], None
    excess = state.from_cad((weight - cap) * state.equity_cad, holding.currency)
    qty = math.floor(excess / holding.price)
    return _plan_sell(run, holding, qty, target, f"trim to {cap:.0%}"), None


def plan_account(
    state: AccountState, targets: list[Target], config: PlanConfig, today: date
) -> AccountPlan:
    run = _Run(state, config, today + timedelta(days=config.good_days))
    available = {ccy: max(0.0, cash) for ccy, cash in state.available.items()}
    plan = AccountPlan(state, [], [], available)
    if state.equity_cad <= 0:
        plan.notes.append("no equity in this account; nothing to plan")
        return plan
    _plan_sells(run, targets, plan)
    _plan_buys(run, targets, plan)
    if any(o.side == "Sell" for o in plan.orders):
        plan.notes.append("sale proceeds are not reused here; re-run once sells have filled")
    return plan


def _add(plan: AccountPlan, orders: list[OrderTicket], note: str | None) -> None:
    plan.orders.extend(orders)
    plan.notes.extend([note] if note else [])


def _plan_sells(run: _Run, targets: list[Target], plan: AccountPlan) -> None:
    """Trims, plus exits when asked; holdings outside the strategy otherwise get one note."""
    by_symbol = {t.symbol: t for t in targets}
    outside = [h for h in run.state.holdings if h.symbol not in by_symbol]
    if outside and not run.config.exits:
        plan.notes.append(_outside_note(run.state, outside))
    for holding in run.state.holdings:
        if run.config.exits or holding not in outside:
            _add(plan, *_plan_holding(run, holding, by_symbol.get(holding.symbol)))


def _plan_buys(run: _Run, targets: list[Target], plan: AccountPlan) -> None:
    picked, unfunded = select_targets(run.state, targets, run.config)
    budgets = _budgets(run, picked, plan.cash_left)
    for target, weight in picked:
        budget = budgets.get(target.symbol, 0.0)
        _add(plan, *_plan_buy(run, target, weight, budget, plan.cash_left))
    plan.notes.extend(
        f"{t.symbol} ({t.weight:.0%} of the strategy) not opened: no {t.currency} cash; "
        f"convert currency to fund it"
        for t in unfunded
    )


def plan_all(
    states: list[AccountState], targets: list[Target], config: PlanConfig, today: date
) -> list[AccountPlan]:
    return [plan_account(state, targets, config, today) for state in states]
