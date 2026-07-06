from ibkr_trader.accounts import AccountType, get_profile, security_domicile


def test_effective_withholding_by_account():
    # registered retirement plans: US dividends treaty-exempt
    assert get_profile(AccountType.RRSP).effective_us_withholding() == 0.0
    assert get_profile(AccountType.LIRA).effective_us_withholding() == 0.0
    # TFSA/FHSA: 15% non-recoverable
    assert get_profile(AccountType.TFSA).effective_us_withholding() == 0.15
    assert get_profile(AccountType.FHSA).effective_us_withholding() == 0.15
    # non-registered: recoverable via foreign tax credit → no drag in this model
    assert get_profile(AccountType.NONREG).effective_us_withholding() == 0.0


def test_registered_and_locked_in_flags():
    assert get_profile(AccountType.NONREG).registered is False
    assert all(get_profile(a).registered for a in (AccountType.RRSP, AccountType.TFSA))
    assert get_profile(AccountType.LIRA).locked_in is True
    assert get_profile(AccountType.RRSP).locked_in is False


def test_get_profile_accepts_string():
    assert get_profile("tfsa").account is AccountType.TFSA


def test_security_domicile():
    assert security_domicile("USD") == "US"
    assert security_domicile("cad") == "CA"
    assert security_domicile("EUR") == "OTHER"
