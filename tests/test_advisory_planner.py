"""The order planner: every rule the owner set, one test (or more) each. Pure, no I/O."""

from datetime import date

import pytest

from ibkr_trader.accounts import AccountType
from ibkr_trader.advisory.planner import (
    AccountPlan,
    AccountState,
    Holding,
    OrderTicket,
    PlanConfig,
    Pricing,
    Target,
    cap_weights,
    ladder,
    plan_account,
    plan_all,
    round_to_tick,
    select_targets,
)

TODAY = date(2026, 10, 18)
CONFIG = PlanConfig()


def _state(
    *,
    cad: float = 10_000.0,
    usd: float = 0.0,
    equity: float | None = None,
    holdings: tuple[Holding, ...] = (),
    kind: AccountType = AccountType.TFSA,
    **extra,
) -> AccountState:
    held = sum(h.value * (1.4 if h.currency == "USD" else 1.0) for h in holdings)
    return AccountState(
        number="12345282",
        kind=kind,
        label=kind.value.upper(),
        equity_cad=equity if equity is not None else cad + usd * 1.4 + held,
        usdcad=1.4,
        available={"CAD": cad, "USD": usd},
        holdings=holdings,
        **extra,
    )


def _target(symbol: str, weight: float, price: float = 50.0, **extra) -> Target:
    currency = "CAD" if symbol.endswith(".TO") else "USD"
    return Target(symbol=symbol, currency=currency, weight=weight, price=price, **extra)


def _buys(plan: AccountPlan) -> list[OrderTicket]:
    return [o for o in plan.orders if o.side == "Buy"]


def _spent(plan: AccountPlan, currency: str) -> float:
    return sum(o.notional for o in _buys(plan) if o.currency == currency)


# --- prices and ladders ----------------------------------------------------------------


def test_round_to_tick_never_worsens_the_limit():
    assert round_to_tick(10.019, 0.01, "Buy") == 10.01
    assert round_to_tick(10.011, 0.01, "Sell") == 10.02
    assert round_to_tick(10.02, 0.01, "Buy") == 10.02  # already on the grid
    assert round_to_tick(0.4567, 0.005, "Buy") == 0.455


def test_buy_ladder_steps_below_the_market_and_keeps_every_share():
    rungs = ladder("Buy", 100, Pricing(ref=100.0, vol=0.02, tick=0.01), 0.0, CONFIG)
    assert [q for q, _ in rungs] == [40, 35, 25]
    assert [p for _, p in rungs] == [99.5, 98.5, 97.0]


def test_sell_ladder_steps_above_the_market():
    rungs = ladder("Sell", 10, Pricing(ref=100.0, vol=0.02, tick=0.01), 0.0, CONFIG)
    assert [p for _, p in rungs] == [100.5, 101.5, 103.0]
    assert sum(q for q, _ in rungs) == 10


def test_ladder_never_has_an_empty_rung_and_falls_back_to_one():
    pricing = Pricing(ref=100.0, vol=0.02, tick=0.01)
    assert ladder("Buy", 2, pricing, 0.0, CONFIG) == [(2, 99.5)]  # 2/0 split: one rung
    assert ladder("Buy", 50, pricing, 1e9, CONFIG) == [(50, 99.5)]


def test_every_rung_clears_the_minimum_after_rounding_to_whole_shares():
    """14 shares in three rungs is 7/4/3, and 3 x 97 = 291 is under a 300 floor -- even
    though 25% of the order's value is not. The dry run on real accounts showed this."""
    rungs = ladder("Buy", 14, Pricing(ref=100.0, vol=0.02, tick=0.01), 300.0, CONFIG)
    assert rungs == [(8, 99.5), (6, 98.5)]
    assert all(q * p >= 300.0 for q, p in rungs)


# --- weights ---------------------------------------------------------------------------


def test_cap_weights_spreads_the_excess_and_leaves_cash_when_everything_is_capped():
    capped = cap_weights({"A": 0.6, "B": 0.2, "ETF": 0.2}, {"A", "B"}, 0.25)
    assert capped["A"] == pytest.approx(0.25)
    assert capped["B"] == pytest.approx(0.25)
    assert capped["ETF"] == pytest.approx(0.5)
    all_stocks = cap_weights({"A": 0.5, "B": 0.5}, {"A", "B"}, 0.2)
    assert all_stocks == {"A": pytest.approx(0.2), "B": pytest.approx(0.2)}


def test_small_account_concentrates_into_fewer_larger_names():
    targets = [_target(f"S{i}.TO", 0.1) for i in range(10)]
    picked, _ = select_targets(_state(cad=3_000.0), targets, PlanConfig(max_position_pct=1.0))
    assert len(picked) == 3
    assert sum(w for _, w in picked) == pytest.approx(1.0)


def test_at_most_max_new_names_are_opened_per_run():
    targets = [_target(f"S{i}.TO", 0.1) for i in range(10)]
    picked, _ = select_targets(_state(cad=100_000.0), targets, PlanConfig(max_new_names=2))
    assert len(picked) == 2


def test_tfsa_prefers_the_cad_listing_when_weights_tie():
    targets = [_target("SPY", 0.5), _target("XIC.TO", 0.5)]
    tfsa, _ = select_targets(_state(cad=5_000.0, usd=5_000.0), targets, PlanConfig(max_new_names=1))
    rrsp, _ = select_targets(
        _state(cad=5_000.0, usd=5_000.0, kind=AccountType.RRSP),
        targets,
        PlanConfig(max_new_names=1),
    )
    assert [t.symbol for t, _ in tfsa] == ["XIC.TO"]
    assert [t.symbol for t, _ in rrsp] == ["SPY"]  # no withholding drag: alphabetical tie


def test_a_name_the_account_cannot_fund_does_not_use_a_new_name_slot():
    """The real TFSA held USD only: XIC.TO took a slot and SPY's neighbour went unopened."""
    targets = [_target("XIC.TO", 0.5), _target("SPY", 0.3), _target("AAPL", 0.2)]
    picked, unfunded = select_targets(
        _state(cad=0.0, usd=10_000.0), targets, PlanConfig(max_new_names=1)
    )
    assert [t.symbol for t, _ in picked] == ["SPY"]
    assert [t.symbol for t in unfunded] == ["XIC.TO"]


# --- buying --------------------------------------------------------------------------


def test_buys_never_exceed_the_cash_in_their_currency():
    targets = [_target("XIC.TO", 0.5, broad_etf=True), _target("SPY", 0.5, broad_etf=True)]
    plan = plan_account(_state(cad=10_000.0, usd=0.0), targets, CONFIG, TODAY)

    assert _spent(plan, "CAD") <= 10_000.0
    assert _spent(plan, "USD") == 0
    assert any("SPY (50% of the strategy) not opened: no USD cash" in n for n in plan.notes)
    assert plan.cash_left["CAD"] == pytest.approx(10_000.0 - _spent(plan, "CAD"))


def test_new_names_share_scarce_cash_in_proportion_to_their_gaps():
    """The real TFSA put USD 201k into SPY and left EFA a USD 1.4k remnant."""
    held = (Holding("XEQT.TO", "CAD", 1_000, 140.0),)  # outside: makes the cash scarce
    targets = [_target("SPY", 0.6, broad_etf=True), _target("EFA", 0.4, broad_etf=True)]
    plan = plan_account(_state(cad=0.0, usd=100_000.0, holdings=held), targets, CONFIG, TODAY)

    spy = sum(o.notional for o in _buys(plan) if o.symbol == "SPY")
    efa = sum(o.notional for o in _buys(plan) if o.symbol == "EFA")
    assert spy + efa <= 100_000.0
    assert efa / (spy + efa) == pytest.approx(0.4, abs=0.01)


def test_a_gap_whose_share_is_below_the_minimum_yields_its_cash_to_the_rest():
    held = (Holding("XEQT.TO", "CAD", 110, 50.0),)  # equity 7,000 with 1,500 cash
    targets = [_target("A.TO", 0.8, broad_etf=True), _target("B.TO", 0.2, broad_etf=True)]
    plan = plan_account(_state(cad=1_500.0, holdings=held), targets, CONFIG, TODAY)

    assert {o.symbol for o in _buys(plan)} == {"A.TO"}
    assert sum(o.notional for o in _buys(plan)) > 1_400.0
    assert any(n.startswith("B.TO: wants CAD") for n in plan.notes)


def test_an_incumbent_is_funded_before_a_heavier_new_name():
    # The XEQT outside the strategy makes cash the binding constraint: the targets want
    # 2,000 more AAA and 4,500 of BIG, and there is 1,500.
    held = (Holding("AAA.TO", "CAD", 20, 50.0), Holding("XEQT.TO", "CAD", 100, 50.0))
    targets = [_target("BIG.TO", 0.6), _target("AAA.TO", 0.4)]
    plan = plan_account(
        _state(cad=1_500.0, holdings=held), targets, PlanConfig(max_position_pct=1.0), TODAY
    )
    assert {o.symbol for o in _buys(plan)} == {"AAA.TO"}
    assert all("top up" in o.note for o in _buys(plan))
    assert any(n.startswith("BIG.TO: wants CAD") for n in plan.notes)


def test_every_order_is_at_least_the_minimum():
    targets = [_target(f"S{i}.TO", 0.25, broad_etf=True) for i in range(4)]
    plan = plan_account(_state(cad=20_000.0), targets, CONFIG, TODAY)
    per_name: dict[str, float] = {}
    for o in _buys(plan):
        per_name[o.symbol] = per_name.get(o.symbol, 0.0) + o.notional
    assert per_name and all(total >= CONFIG.min_order_cad * 0.97 for total in per_name.values())
    assert all(o.notional >= CONFIG.min_rung_cad * 0.97 for o in _buys(plan))


def test_a_held_name_near_its_target_gets_no_tiny_top_up():
    held = (Holding("XIC.TO", "CAD", 190, 50.0),)  # 9,500 of a 10,000 account
    plan = plan_account(
        _state(cad=500.0, holdings=held), [_target("XIC.TO", 1.0, broad_etf=True)], CONFIG, TODAY
    )
    assert plan.orders == []


def test_an_open_buy_order_counts_as_held_so_a_rerun_does_not_double_buy():
    targets = [_target("XIC.TO", 1.0, broad_etf=True)]
    fresh = plan_account(_state(cad=10_000.0), targets, CONFIG, TODAY)
    rerun = plan_account(
        _state(cad=500.0, equity=10_000.0, pending_buys={"XIC.TO": 9_500.0}), targets, CONFIG, TODAY
    )
    assert _buys(fresh)
    assert _buys(rerun) == []


def test_tickets_carry_limit_prices_below_the_quote_and_a_good_till_date():
    plan = plan_account(
        _state(cad=10_000.0),
        [_target("XIC.TO", 1.0, broad_etf=True, daily_vol=0.01)],
        CONFIG,
        TODAY,
    )
    assert _buys(plan)
    assert all(o.limit_price < 50.0 for o in _buys(plan))
    assert all(o.good_until == date(2026, 10, 25) for o in plan.orders)
    assert all(o.account == "TFSA ...282" for o in plan.orders)


def test_a_delayed_quote_is_flagged_on_the_ticket():
    plan = plan_account(
        _state(), [_target("XIC.TO", 1.0, broad_etf=True, delayed=True)], CONFIG, TODAY
    )
    assert all("DELAYED" in o.note for o in plan.orders)


# --- selling ---------------------------------------------------------------------------


def test_a_stock_past_its_cap_is_trimmed_but_a_broad_etf_is_not():
    stock = Holding("BANK.TO", "CAD", 100, 100.0)  # 10,000 of 20,000 = 50%
    etf = Holding("XEQT.TO", "CAD", 300, 33.0)
    targets = [_target("BANK.TO", 0.5, price=100.0), _target("XEQT.TO", 0.5, broad_etf=True)]

    plan = plan_account(_state(cad=100.0, holdings=(stock, etf)), targets, CONFIG, TODAY)

    sells = [o for o in plan.orders if o.side == "Sell"]
    assert {o.symbol for o in sells} == {"BANK.TO"}
    assert sum(o.quantity for o in sells) == 60  # 50% -> 20% of 20,000 equity
    assert all(o.limit_price > 100.0 for o in sells)
    assert any("sale proceeds are not reused" in n for n in plan.notes)


def test_a_holding_outside_the_strategy_is_noted_and_only_sold_with_exits():
    held = (Holding("XEQT.TO", "CAD", 300, 33.0),)
    kept = plan_account(_state(cad=0.0, holdings=held), [], CONFIG, TODAY)
    sold = plan_account(_state(cad=0.0, holdings=held), [], PlanConfig(exits=True), TODAY)

    assert kept.orders == []
    assert kept.notes == ["1 holding(s) outside the strategy (100%), left alone: XEQT.TO 100%"]
    assert sum(o.quantity for o in sold.orders if o.side == "Sell") == 300


def test_an_open_sell_order_blocks_a_second_trim():
    stock = Holding("BANK.TO", "CAD", 100, 100.0)
    plan = plan_account(
        _state(cad=0.0, holdings=(stock,), pending_sells=frozenset({"BANK.TO"})),
        [_target("BANK.TO", 1.0, price=100.0)],
        CONFIG,
        TODAY,
    )
    assert [o for o in plan.orders if o.side == "Sell"] == []
    assert any("open sell order" in n for n in plan.notes)


# --- accounts --------------------------------------------------------------------------


def test_an_empty_account_plans_nothing():
    plan = plan_account(_state(cad=0.0, equity=0.0, kind=AccountType.NONREG), [], CONFIG, TODAY)
    assert plan.orders == [] and plan.notes == ["no equity in this account; nothing to plan"]


def test_plan_all_returns_one_plan_per_account_in_order():
    states = [_state(), _state(kind=AccountType.RRSP)]
    plans = plan_all(states, [_target("XIC.TO", 1.0, broad_etf=True)], CONFIG, TODAY)
    assert [p.account.kind for p in plans] == [AccountType.TFSA, AccountType.RRSP]


def test_account_state_converts_usd_and_values_holdings():
    holding = Holding("AAPL", "USD", 2, 100.0)
    state = _state(cad=0.0, holdings=(holding,), pending_buys={"MSFT": 50.0})
    assert holding.value == 200.0
    assert state.to_cad(100.0, "USD") == pytest.approx(140.0)
    assert state.from_cad(140.0, "USD") == pytest.approx(100.0)
    assert state.to_cad(100.0, "CAD") == 100.0
    assert state.owned() == {"AAPL", "MSFT"}
    assert state.held_value("AAPL") == 200.0 and state.held_value("MSFT") == 50.0
    assert state.masked == "TFSA ...282"
    ticket = OrderTicket("a", "Buy", "X", 3, 10.0, "CAD", TODAY, "")
    assert ticket.notional == 30.0
