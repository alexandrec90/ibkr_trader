"""Read-only Questrade client: token rotation, redaction, throttling and response parsing.

No test touches the network: the client takes its transport as a parameter, and the one
test of the real transport stubs ``urlopen``.
"""

import io
import urllib.error
import urllib.parse
from datetime import UTC, datetime

import pytest

from ibkr_trader.advisory import questrade as qt

AUTH = {
    "access_token": "access-1",
    "api_server": "https://api05.iq.questrade.com",
    "refresh_token": "refresh-new",
    "expires_in": 1800,
    "token_type": "Bearer",
}


class FakeTransport:
    """Answers by URL substring; records every (url, headers) it was asked for."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> dict:
        self.calls.append((url, headers))
        for fragment, body in self.routes.items():
            if fragment in url:
                if isinstance(body, Exception):
                    raise body
                assert isinstance(body, dict)
                return body
        raise AssertionError(f"unexpected request {url}")


def _store(tmp_path, text: str = "A=1\r\nQUESTRADE_TOKEN=refresh-old\r\nB=2\r\n"):
    path = tmp_path / ".env"
    path.write_bytes(text.encode())
    return qt.EnvTokenStore(path)


def _client(routes: dict[str, object]) -> tuple[qt.QuestradeClient, FakeTransport]:
    transport = FakeTransport(routes)
    client = qt.QuestradeClient(
        api_server="https://api05.iq.questrade.com/",
        access_token="access-1",
        transport=transport,
        sleep=lambda _: None,
    )
    return client, transport


# --- the token store -----------------------------------------------------------------


def test_store_loads_the_value_without_quotes(tmp_path):
    assert _store(tmp_path, 'QUESTRADE_TOKEN="abc"\n').load() == "abc"
    assert _store(tmp_path, "OTHER=1\n").load() is None
    assert _store(tmp_path, "QUESTRADE_TOKEN=\n").load() is None
    assert qt.EnvTokenStore(tmp_path / "absent.env").load() is None


def test_store_save_rewrites_only_the_token_line_and_keeps_crlf(tmp_path):
    store = _store(tmp_path)
    store.save("refresh-new")
    assert store.path.read_bytes() == b"A=1\r\nQUESTRADE_TOKEN=refresh-new\r\nB=2\r\n"


def test_store_save_appends_the_key_when_the_file_lacks_it(tmp_path):
    store = _store(tmp_path, "A=1")
    store.save("tok")
    assert store.path.read_text() == "A=1\nQUESTRADE_TOKEN=tok\n"


def test_store_check_writable_passes_on_a_writable_file(tmp_path):
    _store(tmp_path).check_writable()
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


# --- login -----------------------------------------------------------------------------


def test_connect_saves_the_rotated_token_before_any_api_call(tmp_path):
    store = _store(tmp_path)
    transport = FakeTransport({"login.questrade.com": AUTH})

    client = qt.QuestradeClient.connect(store, "refresh-old", transport)

    url, headers = transport.calls[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith(qt.LOGIN_URL)
    assert query == {"grant_type": ["refresh_token"], "refresh_token": ["refresh-old"]}
    assert headers == {}
    assert store.load() == "refresh-new"
    assert client.api_server == "https://api05.iq.questrade.com/"
    assert client.access_token == "access-1"


def test_failed_login_redacts_the_token_and_says_how_to_get_a_new_one(tmp_path):
    store = _store(tmp_path)
    error = qt.QuestradeError("HTTP 400 from /oauth2/token: bad token refresh-old")
    transport = FakeTransport({"login.questrade.com": error})

    with pytest.raises(qt.QuestradeError) as caught:
        qt.QuestradeClient.connect(store, "refresh-old", transport)

    assert "refresh-old" not in str(caught.value)
    assert "New manual authorization" in str(caught.value)
    assert store.load() == "refresh-old"


def test_connect_refuses_before_spending_the_token_when_env_is_not_writable(tmp_path, monkeypatch):
    def read_only(self):
        raise PermissionError("read-only")

    monkeypatch.setattr(qt.EnvTokenStore, "check_writable", read_only)
    transport = FakeTransport({"login.questrade.com": AUTH})

    with pytest.raises(PermissionError):
        qt.QuestradeClient.connect(_store(tmp_path), "refresh-old", transport)
    assert transport.calls == []


# --- the transport ---------------------------------------------------------------------


def test_urllib_get_sends_a_get_with_a_browser_user_agent(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(qt.urllib.request, "urlopen", fake_urlopen)

    assert qt.urllib_get("https://x.test/v1/accounts", {"Authorization": "Bearer a"}) == {
        "ok": True
    }
    assert seen[0].get_method() == "GET"
    assert seen[0].data is None
    assert seen[0].get_header("User-agent") == qt.USER_AGENT
    assert seen[0].get_header("Authorization") == "Bearer a"


def test_urllib_get_errors_name_the_path_but_never_the_query(monkeypatch):
    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b"no"))

    monkeypatch.setattr(qt.urllib.request, "urlopen", refuse)

    with pytest.raises(qt.QuestradeError) as caught:
        qt.urllib_get(f"{qt.LOGIN_URL}?refresh_token=secret", {})
    assert "HTTP 403 from /oauth2/token: no" == str(caught.value)


def test_urllib_get_wraps_unreachable_hosts(monkeypatch):
    def down(request, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(qt.urllib.request, "urlopen", down)
    with pytest.raises(qt.QuestradeError, match="cannot reach /v1/time"):
        qt.urllib_get("https://x.test/v1/time", {})


def test_get_throttles_under_the_rate_limit_and_sends_the_bearer():
    times = iter([0.0, 0.0, 0.03, 0.1])
    slept: list[float] = []
    transport = FakeTransport({"v1/": {}})
    client = qt.QuestradeClient(
        api_server="https://api.test/",
        access_token="access-1",
        transport=transport,
        clock=lambda: next(times),
        sleep=slept.append,
    )

    client.get("time")
    client.get("accounts", {"a": "1"})

    assert slept == [pytest.approx(qt.MIN_INTERVAL_SECONDS - 0.03)]
    assert [url for url, _ in transport.calls] == [
        "https://api.test/v1/time",
        "https://api.test/v1/accounts?a=1",
    ]
    assert transport.calls[0][1] == {"Authorization": "Bearer access-1"}


# --- response parsing ------------------------------------------------------------------


def test_accounts_parse_number_type_and_status():
    client, _ = _client(
        {"v1/accounts": {"accounts": [{"number": 123, "type": "TFSA", "status": "Closed"}]}}
    )
    assert qt.accounts(client) == [qt.QtAccount("123", "TFSA", "Closed")]


def test_balances_split_per_currency_and_combined():
    row = {"currency": "CAD", "cash": 10.5, "buyingPower": 9.0, "totalEquity": 100.0}
    client, _ = _client(
        {"balances": {"perCurrencyBalances": [row], "combinedBalances": [{**row, "cash": None}]}}
    )
    per_currency, combined = qt.balances(client, "123")
    assert per_currency["CAD"] == qt.QtBalance("CAD", 10.5, 9.0, 100.0)
    assert combined["CAD"].cash == 0.0


def test_positions_skip_closed_lines():
    client, _ = _client(
        {
            "positions": {
                "positions": [
                    {
                        "symbol": "XEQT.TO",
                        "symbolId": 1,
                        "openQuantity": 10,
                        "currentPrice": 30,
                        "currentMarketValue": 300,
                    },
                    {"symbol": "OLD", "symbolId": 2, "openQuantity": 0},
                ]
            }
        }
    )
    assert qt.positions(client, "123") == [qt.QtPosition("XEQT.TO", 1, 10.0, 30.0, 300.0)]


def test_open_orders_look_back_a_month_not_just_today():
    order = {
        "symbol": "AAPL",
        "symbolId": 8049,
        "side": "Buy",
        "openQuantity": 5,
        "limitPrice": 100.0,
    }
    client, transport = _client({"orders": {"orders": [order, {**order, "limitPrice": None}]}})
    now = datetime(2026, 10, 18, 15, tzinfo=UTC)

    orders = qt.open_orders(client, "123", now=now)

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0][0]).query)
    assert query["stateFilter"] == ["Open"]
    assert query["startTime"] == ["2026-09-18T15:00:00+00:00"]
    assert orders[0] == qt.QtOpenOrder("AAPL", 8049, "Buy", 5.0, 100.0)
    assert orders[1].limit_price is None


def test_symbol_by_name_keeps_only_an_exact_match_and_survives_a_refusal():
    row = {
        "symbol": "XEQT.TO",
        "symbolId": 7,
        "currency": "CAD",
        "listingExchange": "TSX",
        "isTradable": True,
        "prevDayClosePrice": 35.2,
        "minTicks": [{"pivot": 0, "minTick": 0.005}, {"pivot": 0.5, "minTick": 0.01}],
    }
    client, _ = _client({"names=XEQT.TO": {"symbols": [row]}, "names=XEQ": {"symbols": [row]}})
    found = qt.symbol_by_name(client, "XEQT.TO")
    assert found is not None and found.symbol_id == 7 and found.prev_close == 35.2
    assert qt.symbol_by_name(client, "XEQ") is None

    refusing, _ = _client({"symbols": qt.QuestradeError("HTTP 400")})
    assert qt.symbol_by_name(refusing, "NOPE") is None


def test_tick_for_picks_the_band_the_price_falls_in():
    symbol = qt.QtSymbol("X", 1, "CAD", "TSX", True, None, ((0.0, 0.005), (0.5, 0.01)))
    assert symbol.tick_for(0.4) == 0.005
    assert symbol.tick_for(35.0) == 0.01
    assert qt.QtSymbol("Y", 2, "USD", "NYSE", True, None).tick_for(5.0) == 0.01


def test_symbols_by_id_and_quotes_batch_ids_and_skip_empty_requests():
    client, transport = _client(
        {
            "markets/quotes": {
                "quotes": [
                    {
                        "symbolId": 7,
                        "lastTradePrice": 35.0,
                        "bidPrice": 0,
                        "askPrice": 35.1,
                        "delay": 1,
                    }
                ]
            },
            "symbols?ids": {"symbols": [{"symbol": "XEQT.TO", "symbolId": 7, "currency": "CAD"}]},
        }
    )
    assert qt.symbols_by_id(client, []) == {} and qt.quotes(client, []) == {}
    assert transport.calls == []

    assert qt.symbols_by_id(client, [7, 8])[7].currency == "CAD"
    quote = qt.quotes(client, [7])[7]
    assert isinstance(quote, qt.QtQuote)
    assert (quote.last, quote.bid, quote.ask, quote.delayed) == (35.0, None, 35.1, True)
    assert "ids=7%2C8" in transport.calls[0][0]
