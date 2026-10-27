"""Read-only Questrade API client for a personal app.

Facts this module relies on, from Questrade's API documentation
(https://www.questrade.com/api/documentation, read 2026-10-18):

- A personal app gets a manual refresh token from the API Centre. Redeeming it at
  ``login.questrade.com/oauth2/token`` returns an access token, the ``api_server`` to call
  and a **new** refresh token; the redeemed one stops working. The new one is written back
  to ``.env`` before any other call, so a failure later in the run cannot strand the owner
  with a spent token. An unused token expires 7 days after it was generated.
- Order placement is for partner apps only. This client cannot reach it: every request
  it makes is a GET.
- Rate limits: 30 req/s for account calls, 20 req/s for market data. One request per
  ``MIN_INTERVAL_SECONDS`` keeps every run under both.
- Without a real-time data subscription a quote is a "snap quote", and once a per-market
  limit is spent the API returns delayed data; ``delay`` says which.

Questrade's edge answers Python's default User-Agent with HTTP 403 before the request
reaches the API (observed 2026-10-18), so every request sends a browser-like one.
"""

import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

LOGIN_URL = "https://login.questrade.com/oauth2/token"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ibkr-trader"
TOKEN_KEY = "QUESTRADE_TOKEN"
MIN_INTERVAL_SECONDS = 0.1
#: How far back open orders are read. The endpoint defaults to today only, which would
#: miss a good-till order placed earlier in the week and plan the same buy twice.
OPEN_ORDER_LOOKBACK_DAYS = 30
TOKEN_HELP = (
    "a Questrade token works once and expires 7 days after it was generated: in the API "
    "Centre open Personal applications, click 'New manual authorization', then 'Generate "
    f"new token for manual authorization', and paste it into {TOKEN_KEY} in .env"
)

#: ``url, headers -> parsed JSON``. Injected so tests never touch the network.
Transport = Callable[[str, dict[str, str]], dict]


class QuestradeError(RuntimeError):
    """A failed Questrade call. Messages never contain a token."""


def urllib_get(url: str, headers: dict[str, str]) -> dict:
    """GET ``url`` and parse the JSON body; any failure becomes a ``QuestradeError``.

    The message names the path only: the login URL carries the refresh token in its query.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
    )
    path = urllib.parse.urlsplit(url).path
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise QuestradeError(f"HTTP {exc.code} from {path}: {body}") from None
    except urllib.error.URLError as exc:
        raise QuestradeError(f"cannot reach {path}: {exc.reason}") from None


@dataclass(frozen=True)
class EnvTokenStore:
    """The refresh token's home: one ``KEY=value`` line in a dotenv file.

    The token rotates on every redemption, so it lives where it can be rewritten, and the
    rewrite touches that one line and nothing else in the file.
    """

    path: Path
    key: str = TOKEN_KEY

    def load(self) -> str | None:
        if not self.path.is_file():
            return None
        with open(self.path, encoding="utf-8", newline="") as handle:
            for line in handle.read().splitlines():
                name, sep, value = line.partition("=")
                if sep and name.strip() == self.key:
                    return value.strip().strip('"').strip("'") or None
        return None

    def check_writable(self) -> None:
        """Fail before the token is spent, not after, if the rotation could not be saved."""
        probe_fd, probe = tempfile.mkstemp(dir=self.path.parent, prefix=".env.probe.")
        os.close(probe_fd)
        os.remove(probe)
        with open(self.path, "r+", encoding="utf-8"):
            pass

    def save(self, token: str) -> None:
        """Replace the key's line (or append one), atomically, keeping the file's line ends."""
        text = ""
        if self.path.is_file():
            with open(self.path, encoding="utf-8", newline="") as handle:
                text = handle.read()
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            name, sep, _ = line.partition("=")
            if sep and name.strip() == self.key:
                ending = line[len(line.rstrip("\r\n")) :] or "\n"
                lines[index] = f"{self.key}={token}{ending}"
                break
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"{self.key}={token}\n")
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".env.tmp.")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write("".join(lines))
        os.replace(tmp, self.path)


@dataclass
class QuestradeClient:
    """An authenticated, throttled, GET-only session against one ``api_server``."""

    api_server: str
    access_token: str
    transport: Transport = urllib_get
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _last_call: float = field(default=float("-inf"), repr=False)

    @classmethod
    def connect(
        cls, store: EnvTokenStore, token: str, transport: Transport = urllib_get
    ) -> "QuestradeClient":
        """Redeem ``token`` and persist its replacement to ``store`` before returning."""
        store.check_writable()
        query = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": token})
        try:
            auth = transport(f"{LOGIN_URL}?{query}", {})
        except QuestradeError as exc:
            message = str(exc).replace(token, "<redacted>")
            raise QuestradeError(f"login failed ({message}); {TOKEN_HELP}") from None
        store.save(auth["refresh_token"])
        return cls(
            api_server=auth["api_server"].rstrip("/") + "/",
            access_token=auth["access_token"],
            transport=transport,
        )

    def get(self, path: str, params: dict[str, str] | None = None) -> dict:
        wait = self._last_call + MIN_INTERVAL_SECONDS - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._last_call = self.clock()
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        headers = {"Authorization": f"Bearer {self.access_token}"}
        return self.transport(f"{self.api_server}v1/{path}{query}", headers)


@dataclass(frozen=True)
class QtAccount:
    number: str
    type: str
    status: str = "Active"


@dataclass(frozen=True)
class QtBalance:
    currency: str
    cash: float
    buying_power: float
    total_equity: float


@dataclass(frozen=True)
class QtPosition:
    symbol: str
    symbol_id: int
    quantity: float
    price: float
    market_value: float


@dataclass(frozen=True)
class QtOpenOrder:
    symbol: str
    symbol_id: int
    side: str
    open_quantity: float
    limit_price: float | None


@dataclass(frozen=True)
class QtSymbol:
    symbol: str
    symbol_id: int
    currency: str
    listing_exchange: str
    tradable: bool
    prev_close: float | None
    #: ``(pivot, min_tick)`` pairs: ``min_tick`` applies from ``pivot`` upwards.
    min_ticks: tuple[tuple[float, float], ...] = ()

    def tick_for(self, price: float) -> float:
        applicable = [tick for pivot, tick in sorted(self.min_ticks) if price >= pivot]
        return applicable[-1] if applicable else 0.01


@dataclass(frozen=True)
class QtQuote:
    symbol_id: int
    last: float | None
    bid: float | None
    ask: float | None
    delayed: bool


def _positive(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and value > 0 else None


def accounts(client: QuestradeClient) -> list[QtAccount]:
    return [
        QtAccount(
            number=str(row["number"]),
            type=str(row["type"]),
            status=str(row.get("status", "Active")),
        )
        for row in client.get("accounts").get("accounts", [])
    ]


def balances(client: QuestradeClient, number: str) -> tuple[dict, dict]:
    """``(per_currency, combined)``, each ``{currency: QtBalance}``."""
    body = client.get(f"accounts/{number}/balances")

    def parse(rows: list[dict]) -> dict[str, QtBalance]:
        return {
            row["currency"]: QtBalance(
                currency=row["currency"],
                cash=float(row.get("cash") or 0.0),
                buying_power=float(row.get("buyingPower") or 0.0),
                total_equity=float(row.get("totalEquity") or 0.0),
            )
            for row in rows
        }

    return parse(body.get("perCurrencyBalances", [])), parse(body.get("combinedBalances", []))


def positions(client: QuestradeClient, number: str) -> list[QtPosition]:
    return [
        QtPosition(
            symbol=row["symbol"],
            symbol_id=int(row["symbolId"]),
            quantity=float(row["openQuantity"]),
            price=float(row.get("currentPrice") or 0.0),
            market_value=float(row.get("currentMarketValue") or 0.0),
        )
        for row in client.get(f"accounts/{number}/positions").get("positions", [])
        if float(row.get("openQuantity") or 0) > 0
    ]


def open_orders(
    client: QuestradeClient, number: str, *, now: datetime | None = None
) -> list[QtOpenOrder]:
    now = now or datetime.now(tz=UTC)
    params = {
        "stateFilter": "Open",
        "startTime": (now - timedelta(days=OPEN_ORDER_LOOKBACK_DAYS)).isoformat(),
        "endTime": now.isoformat(),
    }
    return [
        QtOpenOrder(
            symbol=row["symbol"],
            symbol_id=int(row["symbolId"]),
            side=str(row.get("side", "")),
            open_quantity=float(row.get("openQuantity") or 0.0),
            limit_price=_positive(row.get("limitPrice")),
        )
        for row in client.get(f"accounts/{number}/orders", params).get("orders", [])
    ]


def _parse_symbol(row: dict) -> QtSymbol:
    return QtSymbol(
        symbol=row["symbol"],
        symbol_id=int(row["symbolId"]),
        currency=str(row.get("currency", "")),
        listing_exchange=str(row.get("listingExchange", "")),
        tradable=bool(row.get("isTradable", False)),
        prev_close=_positive(row.get("prevDayClosePrice")),
        min_ticks=tuple(
            (float(tick["pivot"]), float(tick["minTick"])) for tick in row.get("minTicks") or []
        ),
    )


def symbol_by_name(client: QuestradeClient, name: str) -> QtSymbol | None:
    """The exact-name match, or None when Questrade does not list it (or refuses the name)."""
    try:
        rows = client.get("symbols", {"names": name}).get("symbols", [])
    except QuestradeError:
        return None
    matches = [_parse_symbol(row) for row in rows if row.get("symbol") == name]
    return matches[0] if matches else None


def symbols_by_id(client: QuestradeClient, ids: list[int]) -> dict[int, QtSymbol]:
    if not ids:
        return {}
    rows = client.get("symbols", {"ids": ",".join(map(str, ids))}).get("symbols", [])
    return {symbol.symbol_id: symbol for symbol in map(_parse_symbol, rows)}


def quotes(client: QuestradeClient, ids: list[int]) -> dict[int, QtQuote]:
    if not ids:
        return {}
    rows = client.get("markets/quotes", {"ids": ",".join(map(str, ids))}).get("quotes", [])
    return {
        int(row["symbolId"]): QtQuote(
            symbol_id=int(row["symbolId"]),
            last=_positive(row.get("lastTradePrice")),
            bid=_positive(row.get("bidPrice")),
            ask=_positive(row.get("askPrice")),
            delayed=bool(row.get("delay")),
        )
        for row in rows
    }
