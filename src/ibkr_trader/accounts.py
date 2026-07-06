"""Registered-account tax profiles.

Québec/Canada registered accounts (RRSP, TFSA, FHSA, LIRA) shelter growth and Canadian
dividends entirely. What differs between them — and the only thing the simulator needs — is how
**foreign (US) dividends** are treated at source, plus whether the account is locked-in.

Key facts (engineering research, not tax advice — see docs/legal-quebec-canada.md, chase the
``[verify]`` markers there before funding anything):

- Canadian-domiciled securities: **no** dividend withholding in any account.
- US-domiciled securities held **directly**:
    - RRSP / LIRA — treaty-exempt (Canada-US treaty, Art. XVIII): withholding ≈ 0.
    - TFSA / FHSA — 15% US withholding applies and is **not recoverable** (the account is
      already tax-sheltered, so there is no Canadian tax to credit it against).
    - Non-registered — 15% withholding applies but is **recoverable** via the foreign tax
      credit, so we treat its dividend drag as ≈ 0 in this pre-withdrawal model.
  Holding US exposure through a *Canadian-listed* ETF breaks the treaty exemption (an
  intermediary sits in the way); modelling that layering is future work — see the strategy doc.

The IBKR **paper** account has no notion of "registered": these rules are simulated here, in
our own cost model, layered on top of paper trading. Nothing in this module transmits an order.
"""

import enum
from dataclasses import dataclass


def security_domicile(currency: str) -> str:
    """Coarse domicile used for withholding: USD listings → 'US', CAD → 'CA', else 'OTHER'.

    A proxy: a USD-priced security is treated as US-domiciled at source. Good enough for the
    withholding model; refine with real domicile once instruments carry it ([verify]).
    """
    return {"USD": "US", "CAD": "CA"}.get(currency.upper(), "OTHER")


class AccountType(enum.StrEnum):
    """The account a simulation is run for. String-valued so it round-trips through JSON
    (persisted into ``backtest_runs.params``) and CLI options cleanly."""

    NONREG = "nonreg"
    RRSP = "rrsp"
    TFSA = "tfsa"
    FHSA = "fhsa"
    LIRA = "lira"


@dataclass(frozen=True)
class AccountTaxProfile:
    """Everything the cost/tax model needs to know about one account type."""

    account: AccountType
    us_dividend_withholding: float  # at-source rate on US-domiciled dividends (0.0-1.0)
    withholding_recoverable: bool  # True only for non-registered (foreign tax credit)
    registered: bool  # growth + Canadian dividends fully sheltered
    locked_in: bool  # LIRA: withdrawals restricted until retirement
    note: str

    def effective_us_withholding(self) -> float:
        """Net US-dividend withholding drag we actually charge in the simulation.

        Recoverable withholding (non-registered) nets to ~0 for an investor who can use the
        foreign tax credit; registered accounts pay whatever cannot be recovered.
        """
        return 0.0 if self.withholding_recoverable else self.us_dividend_withholding


# `[verify]` FHSA and LIRA treaty treatment (see docs/registered-account-strategy.md):
#   - FHSA is a newer plan; whether the IRS/treaty treats it like an RRSP for the US-dividend
#     exemption is unsettled — modelled as NOT exempt (15%), the conservative assumption.
#   - LIRA is a locked-in retirement plan; treated like an RRSP (exempt), but confirm.
PROFILES: dict[AccountType, AccountTaxProfile] = {
    AccountType.NONREG: AccountTaxProfile(
        account=AccountType.NONREG,
        us_dividend_withholding=0.15,
        withholding_recoverable=True,
        registered=False,
        locked_in=False,
        note="Taxable; foreign tax credit recovers US withholding. Baseline only — this is "
        "where the CRA 'carrying on a business' risk lives, so keep turnover low here too.",
    ),
    AccountType.RRSP: AccountTaxProfile(
        account=AccountType.RRSP,
        us_dividend_withholding=0.0,
        withholding_recoverable=False,
        registered=True,
        locked_in=False,
        note="US dividends treaty-exempt when US securities are held directly.",
    ),
    AccountType.TFSA: AccountTaxProfile(
        account=AccountType.TFSA,
        us_dividend_withholding=0.15,
        withholding_recoverable=False,
        registered=True,
        locked_in=False,
        note="US dividends withheld 15%, non-recoverable. Prefer CAD-domiciled US exposure here.",
    ),
    AccountType.FHSA: AccountTaxProfile(
        account=AccountType.FHSA,
        us_dividend_withholding=0.15,
        withholding_recoverable=False,
        registered=True,
        locked_in=False,
        note="Treated like TFSA for US withholding ([verify] treaty status).",
    ),
    AccountType.LIRA: AccountTaxProfile(
        account=AccountType.LIRA,
        us_dividend_withholding=0.0,
        withholding_recoverable=False,
        registered=True,
        locked_in=True,
        note="Locked-in retirement plan; treated like RRSP for US withholding ([verify]).",
    ),
}


def get_profile(account: AccountType | str) -> AccountTaxProfile:
    """Resolve a tax profile from an ``AccountType`` or its string value."""
    key = AccountType(account) if not isinstance(account, AccountType) else account
    return PROFILES[key]
