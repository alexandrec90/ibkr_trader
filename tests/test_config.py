import pytest

from ibkr_trader.accounts import AccountType
from ibkr_trader.config import Settings


def test_account_specific_environment_variables_are_loaded(monkeypatch):
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", "DU1234567")
    monkeypatch.setenv("IBKR_MARGIN_ACCOUNT", "U-MARGIN")
    monkeypatch.setenv("IBKR_RRSP_ACCOUNT", "U-RRSP")
    monkeypatch.setenv("IBKR_TFSA_ACCOUNT", "U-TFSA")
    monkeypatch.setenv("IBKR_FHSA_ACCOUNT", "U-FHSA")

    settings = Settings(_env_file=None)

    assert settings.ibkr_execution_account("tfsa") == "DU1234567"
    assert settings.configured_live_accounts() == {
        AccountType.NONREG: "U-MARGIN",
        AccountType.RRSP: "U-RRSP",
        AccountType.TFSA: "U-TFSA",
        AccountType.FHSA: "U-FHSA",
    }


def test_funded_accounts_map_to_logical_account_types_without_authorizing_execution():
    settings = Settings(
        ibkr_margin_account="U-MARGIN",
        ibkr_rrsp_account="U-RRSP",
        ibkr_tfsa_account="U-TFSA",
        ibkr_fhsa_account="U-FHSA",
        _env_file=None,
    )

    assert settings.configured_live_account_for(AccountType.NONREG) == "U-MARGIN"
    assert settings.configured_live_account_for("rrsp") == "U-RRSP"
    assert settings.configured_live_account_for("tfsa") == "U-TFSA"
    assert settings.configured_live_account_for("fhsa") == "U-FHSA"


def test_missing_funded_account_has_specific_configuration_error():
    settings = Settings(_env_file=None)

    with pytest.raises(RuntimeError, match="IBKR_RRSP_ACCOUNT is not configured"):
        settings.configured_live_account_for("rrsp")


def test_duplicate_funded_account_ids_are_rejected():
    settings = Settings(
        ibkr_margin_account="U-SAME",
        ibkr_tfsa_account="U-SAME",
        _env_file=None,
    )

    with pytest.raises(RuntimeError, match="must be unique"):
        settings.configured_live_accounts()


@pytest.mark.parametrize("logical_account", ["nonreg", "rrsp", "tfsa", "fhsa"])
def test_paper_execution_always_uses_separate_du_account(logical_account):
    settings = Settings(
        environment="paper",
        ibkr_paper_account="DU1234567",
        ibkr_margin_account="U-MARGIN",
        ibkr_rrsp_account="U-RRSP",
        ibkr_tfsa_account="U-TFSA",
        ibkr_fhsa_account="U-FHSA",
        _env_file=None,
    )

    assert settings.ibkr_execution_account(logical_account) == "DU1234567"


def test_paper_execution_refuses_a_funded_account_id():
    settings = Settings(environment="paper", ibkr_paper_account="U1234567", _env_file=None)

    with pytest.raises(RuntimeError, match="starting with DU"):
        settings.ibkr_execution_account("tfsa")


def test_paper_execution_requires_an_account_id():
    settings = Settings(environment="paper", _env_file=None)

    with pytest.raises(RuntimeError, match="IBKR_PAPER_ACCOUNT is not configured"):
        settings.ibkr_execution_account("tfsa")


def test_legacy_singular_paper_account_is_supported_only_when_unambiguous():
    settings = Settings(environment="paper", ibkr_account="DU7654321", _env_file=None)
    assert settings.ibkr_execution_account("tfsa") == "DU7654321"

    conflicting = Settings(
        environment="paper",
        ibkr_paper_account="DU1234567",
        ibkr_account="DU7654321",
        _env_file=None,
    )
    with pytest.raises(RuntimeError, match="disagree"):
        conflicting.ibkr_execution_account("tfsa")


def test_execution_account_refuses_live_routing_even_when_old_ack_gate_is_true():
    settings = Settings(
        environment="live",
        live_trading_acknowledged=True,
        ibkr_paper_account="DU1234567",
        ibkr_tfsa_account="U-TFSA",
        _env_file=None,
    )

    with pytest.raises(RuntimeError, match="live-account routing is disabled"):
        settings.ibkr_execution_account("tfsa")
