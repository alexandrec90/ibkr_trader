"""Planner inputs from live Questrade data, over a fake transport."""

from datetime import UTC, date, datetime

import pytest

from ibkr_trader.accounts import AccountType
from ibkr_trader.advisory import questrade as qt
from ibkr_trader.advisory.sync import (
    account_kind,
    account_state,
    account_states,
    resolve_targets,
    usdcad_from,
)
from ibkr_trader.advisory.targets import StrategyTargets, TargetWeight

NOW = datetime(2026, 10, 18, 15, tzinfo=UTC)


class FakeTransport:
    def __init__(self, routes: dict[str, dict]):
        self.routes = routes
        self.urls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str]) -> dict:
        self.urls.append(url)
        for fragment, body in self.routes.items():
            if fragment in url:
                return body
        raise qt.QuestradeError(f"HTTP 400 from {url}")


def _client(routes: dict[str, dict]) -> qt.QuestradeClient:
    return qt.QuestradeClient(
        api_server="https://api.test/",
        access_token="a",
        transport=FakeTransport(routes),
        sleep=lambda _: None,
    )


def _balance(currency: str, cash: float, power: float, equity: float) -> dict:
    return {"currency": currency, "cash": cash, "buyingPower": power, "totalEquity": equity}


TFSA_ROUTES = {
    "accounts/111/balances": {
        "perCurrencyBalances": [
            _balance("CAD", 5_000, 5_000, 20_000),
            _balance("USD", 100, 100, 100),
        ],
        "combinedBalances": [_balance("CAD", 0, 0, 20_140), _balance("USD", 0, 0, 14_385.71)],
    },
    "accounts/111/positions": {
        "positions": [
            {"symbol": "XEQT.TO", "symbolId": 1, "openQuantity": 400, "currentPrice": 37.5},
            {"symbol": "RY.TO", "symbolId": 2, "openQuantity": 10, "currentPrice": 150.0},
        ]
    },
    "accounts/111/orders": {
        "orders": [
            {
                "symbol": "XIC.TO",
                "symbolId": 3,
                "side": "Buy",
                "openQuantity": 20,
                "limitPrice": 40.0,
            },
            {
                "symbol": "RY.TO",
                "symbolId": 2,
                "side": "Sell",
                "openQuantity": 5,
                "limitPrice": 160.0,
            },
        ]
    },
    "symbols?ids=": {
        "symbols": [
            {"symbol": "XEQT.TO", "symbolId": 1, "currency": "CAD"},
            {"symbol": "XIC.TO", "symbolId": 3, "currency": "CAD"},
        ]
    },
}


def test_account_kind_maps_questrade_types_onto_tax_profiles():
    assert account_kind("TFSA") is AccountType.TFSA
    assert account_kind("SRRSP") is AccountType.RRSP
    assert account_kind("LRRSP") is AccountType.LIRA
    assert account_kind("FHSA") is AccountType.FHSA
    assert account_kind("Margin") is AccountType.NONREG


def test_usdcad_comes_from_the_combined_equity_in_both_currencies():
    combined = {"CAD": qt.QtBalance("CAD", 0, 0, 140.0), "USD": qt.QtBalance("USD", 0, 0, 100.0)}
    assert usdcad_from(combined) == pytest.approx(1.4)
    assert usdcad_from({"CAD": combined["CAD"]}) is None


def test_account_state_nets_open_buys_out_of_cash_and_marks_pending_orders():
    state = account_state(_client(TFSA_ROUTES), qt.QtAccount("111", "TFSA"), now=NOW)

    assert state.kind is AccountType.TFSA
    assert state.equity_cad == 20_140
    assert state.usdcad == pytest.approx(20_140 / 14_385.71)
    assert state.available == {"CAD": 4_200.0, "USD": 100.0}  # 5,000 - 20 x 40 working
    assert state.pending_buys == {"XIC.TO": 800.0}
    assert state.pending_sells == frozenset({"RY.TO"})
    # XEQT's currency comes from its symbol record; RY's record is missing -> the suffix.
    assert {h.symbol: h.currency for h in state.holdings} == {"XEQT.TO": "CAD", "RY.TO": "CAD"}


def test_available_cash_never_exceeds_buying_power_or_goes_negative():
    routes = {
        **TFSA_ROUTES,
        "accounts/111/balances": {
            "perCurrencyBalances": [
                _balance("CAD", 5_000, 1_000, 5_000),
                _balance("USD", -50, 0, 0),
            ],
            "combinedBalances": [],
        },
    }
    state = account_state(_client(routes), qt.QtAccount("111", "TFSA"), now=NOW)
    assert state.available == {"CAD": 1_000.0, "USD": 0.0}
    assert state.equity_cad == 0.0 and state.usdcad == 1.0


def test_account_states_skip_inactive_accounts():
    # Routes match by substring in order, so the per-account ones must precede this one.
    routes = {
        **TFSA_ROUTES,
        "v1/accounts": {
            "accounts": [
                {"number": "111", "type": "TFSA", "status": "Active"},
                {"number": "222", "type": "RRSP", "status": "Closed"},
            ]
        },
    }
    transport = FakeTransport(routes)
    client = qt.QuestradeClient("https://api.test/", "a", transport=transport, sleep=lambda _: None)
    states = account_states(client)
    assert [s.number for s in states] == ["111"]
    assert not any("/222/" in url for url in transport.urls)


def test_resolve_targets_maps_names_prices_them_and_reports_what_it_could_not():
    routes = {
        "names=XIC.TO": {
            "symbols": [
                {
                    "symbol": "XIC.TO",
                    "symbolId": 3,
                    "currency": "CAD",
                    "isTradable": True,
                    "prevDayClosePrice": 40.0,
                    "minTicks": [{"pivot": 0, "minTick": 0.01}],
                }
            ]
        },
        "names=BRK.B": {
            "symbols": [
                {
                    "symbol": "BRK.B",
                    "symbolId": 4,
                    "currency": "USD",
                    "isTradable": True,
                    "prevDayClosePrice": 480.0,
                }
            ]
        },
        "names=SHOP": {
            "symbols": [{"symbol": "SHOP", "symbolId": 5, "currency": "CAD", "isTradable": True}]
        },
        "markets/quotes": {"quotes": [{"symbolId": 3, "lastTradePrice": 41.0, "delay": 0}]},
    }
    targets = StrategyTargets(
        "core",
        date(2026, 10, 1),
        [
            TargetWeight("XIC", "CAD", 0.5, True, 0.01),
            TargetWeight("BRK-B", "USD", 0.3, False, None),
            TargetWeight("SHOP", "USD", 0.2, False, None),  # Questrade answers in CAD
        ],
    )

    resolved, notes = resolve_targets(_client(routes), targets)

    xic, brk = resolved
    assert (xic.symbol, xic.price, xic.delayed, xic.daily_vol) == ("XIC.TO", 41.0, False, 0.01)
    assert (brk.symbol, brk.price, brk.delayed) == ("BRK.B", 480.0, True)  # previous close
    assert notes == ["SHOP: no tradable USD listing on Questrade"]
