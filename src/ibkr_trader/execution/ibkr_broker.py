"""IBKR implementation of Broker using ib_async (docs: https://ib-api-reloaded.github.io/ib_async/).

Order lifecycle notes are summarized in docs/ibkr/04-orders.md — in particular:
- orderStatus callbacks are NOT guaranteed for every transition; reconcile via execDetails
  and periodic reqOpenOrders/reqPositions.
- use whatIf=True for a margin/commission preflight before transmitting.
"""

from ibkr_trader.config import get_settings
from ibkr_trader.execution.broker import Broker, OrderRequest, Position
from ibkr_trader.execution.risk import RiskChecker


class IbkrBroker(Broker):
    def __init__(self):
        self.settings = get_settings()
        self.risk = RiskChecker()
        self._ib = None  # ib_async.IB instance once connected

    def connect(self) -> None:
        # TODO(skeleton):
        #   from ib_async import IB
        #   self._ib = IB()
        #   self._ib.connect(self.settings.ibkr_host, self.settings.ibkr_port,
        #                    clientId=self.settings.ibkr_client_id)
        # Fail loudly if account id doesn't start with "DU" while ENVIRONMENT=paper —
        # that means we accidentally reached a live session.
        raise NotImplementedError

    def disconnect(self) -> None:
        if self._ib is not None:
            self._ib.disconnect()

    def place_order(self, request: OrderRequest) -> int:
        self.settings.assert_trading_allowed()  # paper/live gate — do not remove
        self.risk.check(request)
        # TODO(skeleton):
        #   contract = Stock(request.symbol, "SMART", "USD"); ib.qualifyContracts(contract)
        #   order = MarketOrder(request.side, request.quantity)  # or LimitOrder
        #   preflight: whatIf order → inspect margin impact before transmit
        #   trade = self._ib.placeOrder(contract, order)
        #   persist Order row (environment, ibkr_order_id, permId when it arrives), return id
        raise NotImplementedError

    def cancel_order(self, local_order_id: int) -> None:
        raise NotImplementedError

    def positions(self) -> list[Position]:
        # TODO: self._ib.positions() → [Position(...)]
        raise NotImplementedError

    def account_summary(self) -> dict:
        # TODO: self._ib.accountSummary() → {tag: value}
        raise NotImplementedError
