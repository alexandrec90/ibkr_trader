"""Broker abstraction — paper and live use the exact same interface; only config differs."""

import abc
from dataclasses import dataclass

from ibkr_trader.accounts import AccountType


@dataclass
class OrderRequest:
    symbol: str
    side: str  # BUY | SELL
    quantity: float
    order_type: str = "MKT"  # MKT | LMT
    limit_price: float | None = None
    account: AccountType | str | None = None  # logical wrapper; paper still routes to DU account
    currency: str = "USD"  # listing currency; RiskChecker refuses Canadian listings
    sec_type: str = "STK"
    # The ``instruments`` row the order is for: the ``orders`` audit trail needs it.
    instrument_id: int | None = None
    # The price the caller planned with (e.g. the last stored close), in ``currency``.
    # RiskChecker refuses an order whose live quote strays too far from it, which is how a
    # symbol resolving to the wrong company is caught before anything is sent.
    reference_price: float | None = None


@dataclass
class Position:
    symbol: str
    quantity: float
    avg_cost: float
    currency: str = "USD"


class Broker(abc.ABC):
    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @abc.abstractmethod
    def place_order(self, request: OrderRequest) -> int:
        """Submit an order; returns our local orders.id. Must pass risk checks first."""

    @abc.abstractmethod
    def cancel_order(self, local_order_id: int) -> None: ...

    @abc.abstractmethod
    def positions(self) -> list[Position]: ...

    @abc.abstractmethod
    def account_summary(self) -> dict: ...
