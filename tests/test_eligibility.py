from ibkr_trader.signals.eligibility import (
    Candidate,
    EligibilityLimits,
    is_eligible,
    screen,
)

LIMITS = EligibilityLimits(
    min_price=5.0,
    min_avg_dollar_volume=1_000_000.0,
    min_history_days=252,
)


def _candidate(iid: int, **overrides) -> Candidate:
    base = dict(
        instrument_id=iid,
        symbol=f"SYM{iid}",
        currency="CAD",
        exchange="TSX",
        last_price=50.0,
        avg_dollar_volume=5_000_000.0,
        history_days=1000,
        asset_class="ETF",
        leveraged=False,
    )
    base.update(overrides)
    return Candidate(**base)


def test_screen_accepts_a_clean_blue_chip():
    good = _candidate(1)
    result = screen([good], LIMITS)
    assert result.eligible_ids == {1}
    assert result.excluded == {}


def test_screen_rejects_penny_stock():
    result = screen([_candidate(1, last_price=2.0)], LIMITS)
    assert 1 in result.excluded
    assert "below floor" in result.excluded[1]


def test_screen_rejects_illiquid_and_short_history_and_leveraged_and_foreign():
    candidates = [
        _candidate(1, avg_dollar_volume=100.0),  # illiquid
        _candidate(2, history_days=10),  # too new
        _candidate(3, leveraged=True),  # leveraged ETP
        _candidate(4, currency="EUR"),  # not CAD/USD
        _candidate(5),  # clean
    ]
    result = screen(candidates, LIMITS)
    assert result.eligible_ids == {5}
    assert set(result.excluded) == {1, 2, 3, 4}
    assert "leveraged" in result.excluded[3]
    assert "currency" in result.excluded[4]


def test_is_eligible_matches_screen():
    assert is_eligible(_candidate(1), LIMITS) is True
    assert is_eligible(_candidate(1, last_price=1.0), LIMITS) is False
