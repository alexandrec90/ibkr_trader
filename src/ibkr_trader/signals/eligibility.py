"""Eligibility screen — the hard "what am I allowed to hold" gate.

Registered accounts (and the CRA "not carrying on a business" line) want boring, liquid,
solvent, long-only holdings. This module turns that into an auditable, as-of-date filter:
penny-stock floor, liquidity floor, listing-history minimum, major-exchange/currency, and
exclusion of leveraged/inverse/volatility ETPs. Every rejection carries a human-readable reason
so a backtest can prove *why* something was (in)eligible on a given day — and so the same screen
can gate live orders later (see execution.risk).

Design:
- Works on ``Candidate`` value objects, not DB rows, so it is pure and unit-testable and reuses
  cleanly between the backtester and the pre-trade risk check.
- As-of by construction: the caller computes ``last_price`` / ``avg_dollar_volume`` /
  ``history_days`` using only data at or before the evaluation date (no look-ahead).
- Fundamental solvency screening (market cap, distress/default risk for individual names) needs
  fundamentals ingested first. Until then, the price + liquidity + history + curated-universe
  proxy carries the "nothing likely to default" intent.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

# Currencies/exchanges we consider investable for a Canadian registered account. USD covers
# major US listings; CAD covers the TSX. Extend consciously — anything exotic is out by default.
ALLOWED_CURRENCIES: frozenset[str] = frozenset({"CAD", "USD"})


@dataclass(frozen=True)
class Candidate:
    """As-of view of one instrument the screen and allocators reason about."""

    instrument_id: int
    symbol: str
    currency: str
    exchange: str
    last_price: float  # as-of close, native currency
    avg_dollar_volume: float  # trailing mean of close*volume, native currency
    history_days: int  # bars available up to and including the as-of date
    asset_class: str | None = None  # "STK" | "ETF" (None → unknown, treated as STK)
    leveraged: bool = False  # leveraged / inverse / single-stock / volatility ETP


@dataclass(frozen=True)
class EligibilityLimits:
    """Thresholds for the screen. Deliberately conservative; tune in one place."""

    min_price: float = 5.0  # penny-stock floor (native currency)
    min_avg_dollar_volume: float = 1_000_000.0  # liquidity floor
    min_history_days: int = 252  # ~1y of listing history before we'll hold it
    allowed_currencies: frozenset[str] = ALLOWED_CURRENCIES
    exclude_leveraged: bool = True  # no leveraged/inverse/volatility ETPs


@dataclass
class ScreenResult:
    eligible: list[Candidate] = field(default_factory=list)
    excluded: dict[int, str] = field(default_factory=dict)  # instrument_id -> reason

    @property
    def eligible_ids(self) -> set[int]:
        return {candidate.instrument_id for candidate in self.eligible}


def _rejection_reason(candidate: Candidate, limits: EligibilityLimits) -> str | None:
    """First failing rule for a candidate, or None if it passes everything."""
    if candidate.currency.upper() not in limits.allowed_currencies:
        return f"currency {candidate.currency!r} not in allowed set"
    if limits.exclude_leveraged and candidate.leveraged:
        return "leveraged/inverse/volatility ETP excluded"
    if candidate.last_price < limits.min_price:
        return f"price {candidate.last_price:.2f} below floor {limits.min_price:.2f}"
    if candidate.avg_dollar_volume < limits.min_avg_dollar_volume:
        return (
            f"avg $-volume {candidate.avg_dollar_volume:,.0f} below floor "
            f"{limits.min_avg_dollar_volume:,.0f}"
        )
    if candidate.history_days < limits.min_history_days:
        return f"only {candidate.history_days}d history (< {limits.min_history_days}d)"
    return None


def screen(
    candidates: Sequence[Candidate], limits: EligibilityLimits | None = None
) -> ScreenResult:
    """Partition candidates into eligible / excluded (with reasons) under ``limits``."""
    limits = limits or EligibilityLimits()
    result = ScreenResult()
    for candidate in candidates:
        reason = _rejection_reason(candidate, limits)
        if reason is None:
            result.eligible.append(candidate)
        else:
            result.excluded[candidate.instrument_id] = reason
    return result


def is_eligible(candidate: Candidate, limits: EligibilityLimits | None = None) -> bool:
    """Convenience single-candidate check (used by the pre-trade risk gate)."""
    return _rejection_reason(candidate, limits or EligibilityLimits()) is None
