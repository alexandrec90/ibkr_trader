from unittest.mock import Mock

import pytest

from ibkr_trader.config import Settings
from ibkr_trader.execution import ibkr_broker
from ibkr_trader.execution.broker import OrderRequest


def _broker(monkeypatch, settings):
    monkeypatch.setattr(ibkr_broker, "get_settings", lambda: settings)
    broker = ibkr_broker.IbkrBroker()
    broker.risk = Mock()
    return broker


def test_place_order_checks_risk_and_resolves_logical_account_to_paper(monkeypatch):
    settings = Settings(
        environment="paper",
        ibkr_paper_account="DU1234567",
        ibkr_tfsa_account="U-TFSA",
        _env_file=None,
    )
    broker = _broker(monkeypatch, settings)
    request = OrderRequest(symbol="AAPL", side="BUY", quantity=1, account="tfsa")

    with pytest.raises(NotImplementedError):
        broker.place_order(request)

    broker.risk.check.assert_called_once_with(request)


def test_place_order_never_routes_to_configured_funded_account(monkeypatch):
    settings = Settings(
        environment="paper",
        ibkr_paper_account="U-TFSA",
        ibkr_tfsa_account="U-TFSA",
        _env_file=None,
    )
    broker = _broker(monkeypatch, settings)
    request = OrderRequest(symbol="AAPL", side="BUY", quantity=1, account="tfsa")

    with pytest.raises(RuntimeError, match="starting with DU"):
        broker.place_order(request)

    broker.risk.check.assert_called_once_with(request)


def test_place_order_uses_configured_default_logical_account(monkeypatch):
    settings = Settings(
        environment="paper",
        default_account="rrsp",
        ibkr_paper_account="DU1234567",
        ibkr_rrsp_account="U-RRSP",
        _env_file=None,
    )
    broker = _broker(monkeypatch, settings)
    request = OrderRequest(symbol="AAPL", side="BUY", quantity=1)

    with pytest.raises(NotImplementedError):
        broker.place_order(request)

    broker.risk.check.assert_called_once_with(request)
