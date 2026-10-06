"""The registered-account buy-and-hold strategies: scoring, buffers, mixes, mood tilt."""

from datetime import date
from pathlib import Path

import pytest

from ibkr_trader.signals.eligibility import Candidate
from ibkr_trader.signals.portfolio import (
    BROAD_ETF_SYMBOLS,
    COUCH_POTATO_MIX,
    BufferedStockAllocator,
    CoreSatelliteAllocator,
    CouchPotatoAllocator,
    MoodTiltAllocator,
    RecentMomentumAllocator,
    SteadyCompoundersAllocator,
    available,
    is_fund,
    recency_momentum,
    select_with_buffer,
)

REPO = Path(__file__).resolve().parents[1]


def _cand(iid: int, symbol: str = "", asset_class: str | None = None) -> Candidate:
    return Candidate(
        instrument_id=iid,
        symbol=symbol or f"S{iid}",
        currency="CAD",
        exchange="TSX",
        last_price=50.0,
        avg_dollar_volume=5_000_000.0,
        history_days=1000,
        asset_class=asset_class,
    )


def _momentum_feats(r3=0.10, r6=0.15, r12=0.20, vol=0.20, off_high=-0.05) -> dict[str, float]:
    return {
        "return_3m": r3,
        "return_6m": r6,
        "return_12m": r12,
        "volatility_60d": vol,
        "pct_off_52w_high": off_high,
    }


def test_new_strategies_are_registered():
    names = set(available())
    assert {"recent_momentum", "steady_compounders", "couch_potato", "core_satellite"} <= names
    # needs a mood panel to mean anything, so it is built explicitly, never by name
    assert "mood_tilt" not in names


def test_broad_etf_symbols_mirror_the_etf_universe_file():
    lines = (REPO / "tickers-etfs.txt").read_text(encoding="utf-8").split()
    assert frozenset(s.upper() for s in lines) == BROAD_ETF_SYMBOLS


def test_is_fund_by_asset_class_or_known_symbol():
    assert is_fund(_cand(1, "XEQT"))
    assert is_fund(_cand(2, "NEWETF", asset_class="ETF"))
    assert not is_fund(_cand(3, "RY"))


def test_recency_momentum_weights_the_latest_quarter_most():
    # same 12-month return, but one got it all last quarter and one got it all a year ago
    recent = recency_momentum({"return_3m": 0.21, "return_6m": 0.21, "return_12m": 0.21})
    stale = recency_momentum({"return_3m": 0.0, "return_6m": 0.0, "return_12m": 0.21})
    assert recent is not None and stale is not None
    assert recent == pytest.approx(0.5 * 0.21)
    assert stale == pytest.approx(0.2 * 0.21)
    assert recent > stale


@pytest.mark.parametrize(
    "feats",
    [
        {"return_3m": 0.1, "return_6m": 0.1},
        {"return_3m": -1.0, "return_6m": 0.1, "return_12m": 0.1},
        {},
    ],
)
def test_recency_momentum_missing_or_degenerate_is_none(feats):
    assert recency_momentum(feats) is None


def test_select_with_buffer_keeps_incumbents_inside_the_buffer():
    scores = {1: 10.0, 2: 9.0, 3: 8.0, 4: 7.0, 5: 6.0}
    # fresh book: plain top 2
    assert select_with_buffer(scores, set(), n=2, hold_buffer=2.0) == [1, 2]
    # incumbent 4 is rank 4 ≤ 2×2, so it keeps its seat; best newcomer fills the other
    assert select_with_buffer(scores, {4}, n=2, hold_buffer=2.0) == [4, 1]
    # incumbent 5 is rank 5 > 4: out
    assert select_with_buffer(scores, {5}, n=2, hold_buffer=2.0) == [1, 2]


def test_select_with_buffer_ties_are_deterministic():
    assert select_with_buffer({3: 1.0, 1: 1.0, 2: 1.0}, set(), n=2, hold_buffer=1.0) == [1, 2]


def test_buffered_stock_allocator_requires_a_score_and_equal_weights_its_picks():
    with pytest.raises(NotImplementedError):
        BufferedStockAllocator().score(_cand(1), {})

    class ByPrice(BufferedStockAllocator):
        max_names = 2
        max_weight = 1.0

        def score(self, candidate, feats):
            return feats.get("x")

    weights = ByPrice().allocate(
        [_cand(1), _cand(2), _cand(3), _cand(4, "SPY")],
        {1: {"x": 3.0}, 2: {"x": 2.0}, 3: {"x": 1.0}, 4: {"x": 9.0}},
    )
    assert weights == {1: 0.5, 2: 0.5}  # the fund is skipped despite the top score
    assert ByPrice().allocate([_cand(1)], {1: {"x": -1.0}}) == {}


def test_recent_momentum_skips_funds_broken_trends_and_downtrends():
    alloc = RecentMomentumAllocator()
    candidates = [_cand(1, "XEQT"), _cand(2), _cand(3), _cand(4)]
    features = {
        1: _momentum_feats(),  # a fund: never picked by a stock strategy
        2: _momentum_feats(),
        3: _momentum_feats(off_high=-0.30),  # >20% off its high: trend broken
        4: _momentum_feats(r3=-0.1, r6=-0.1, r12=-0.1),  # negative score
    }
    weights = alloc.allocate(candidates, features)
    # one survivor: equal weight would be 100%, the per-name cap holds it to 12% (rest cash)
    assert weights == {2: pytest.approx(alloc.max_weight)}


def test_recent_momentum_prefers_the_steadier_climb():
    alloc = RecentMomentumAllocator()
    alloc.max_names = 1
    candidates = [_cand(1), _cand(2)]
    features = {1: _momentum_feats(vol=0.40), 2: _momentum_feats(vol=0.15)}
    assert set(alloc.allocate(candidates, features)) == {2}


def test_recent_momentum_buffer_holds_a_fading_incumbent():
    alloc = RecentMomentumAllocator()
    alloc.max_names = 1
    candidates = [_cand(1), _cand(2)]
    first = alloc.allocate(candidates, {1: _momentum_feats(vol=0.1), 2: _momentum_feats(vol=0.3)})
    assert set(first) == {1}
    # 2 now leads, but 1 is still rank 2 ≤ 1 × 4.0 → kept: no trade
    second = alloc.allocate(candidates, {1: _momentum_feats(vol=0.3), 2: _momentum_feats(vol=0.1)})
    assert set(second) == {1}
    # a fresh instance has no incumbents and simply takes the leader
    fresh = RecentMomentumAllocator()
    fresh.max_names = 1
    assert set(
        fresh.allocate(candidates, {1: _momentum_feats(vol=0.3), 2: _momentum_feats(vol=0.1)})
    ) == {2}


def test_recent_momentum_is_long_only_capped_and_at_most_fully_invested():
    alloc = RecentMomentumAllocator()
    candidates = [_cand(i) for i in range(1, 31)]
    features = {i: _momentum_feats(vol=0.1 + i / 100) for i in range(1, 31)}
    weights = alloc.allocate(candidates, features)
    assert len(weights) == alloc.max_names
    assert all(0 < w <= alloc.max_weight for w in weights.values())
    assert sum(weights.values()) <= 1.0 + 1e-9


def _steady_feats(vol=0.15, downside=0.10, r12=0.10, drawdown=-0.10) -> dict[str, float]:
    return {
        "volatility_60d": vol,
        "downside_deviation_252d": downside,
        "return_12m": r12,
        "max_drawdown_252d": drawdown,
    }


def test_steady_compounders_ranks_by_low_recent_risk_and_screens_decliners():
    alloc = SteadyCompoundersAllocator()
    alloc.max_names = 1
    candidates = [_cand(1), _cand(2), _cand(3), _cand(4), _cand(5, "XIC")]
    features = {
        1: _steady_feats(vol=0.25),
        2: _steady_feats(vol=0.12),
        3: _steady_feats(vol=0.05, r12=-0.05),  # calm but falling: out
        4: _steady_feats(vol=0.05, drawdown=-0.40),  # calm now, but crashed this year: out
        5: _steady_feats(vol=0.01),  # a fund: out
    }
    assert set(alloc.allocate(candidates, features)) == {2}


def test_steady_compounders_missing_risk_features_is_skipped():
    alloc = SteadyCompoundersAllocator()
    assert alloc.allocate([_cand(1)], {1: {"return_12m": 0.1, "max_drawdown_252d": -0.1}}) == {}


def _mix_candidates() -> list[Candidate]:
    return [_cand(i, sym) for i, sym in enumerate(COUCH_POTATO_MIX, start=1)]


def test_couch_potato_holds_the_fixed_mix():
    candidates = _mix_candidates()
    weights = CouchPotatoAllocator().allocate(candidates, {})
    by_symbol = {c.symbol: weights[c.instrument_id] for c in candidates}
    assert by_symbol == pytest.approx(COUCH_POTATO_MIX)


def test_couch_potato_renormalizes_when_a_sleeve_is_missing():
    candidates = [c for c in _mix_candidates() if c.symbol != "EEM"]
    weights = CouchPotatoAllocator().allocate(candidates, {})
    assert sum(weights.values()) == pytest.approx(1.0)
    assert CouchPotatoAllocator().allocate([_cand(9, "RY")], {}) == {}


def test_core_satellite_is_seventy_core_thirty_satellite():
    core = _mix_candidates()
    stocks = [_cand(10 + i) for i in range(8)]
    features = {c.instrument_id: _momentum_feats(vol=0.1 + i / 50) for i, c in enumerate(stocks)}
    weights = CoreSatelliteAllocator().allocate(core + stocks, features)
    core_ids = {c.instrument_id for c in core}
    assert sum(w for iid, w in weights.items() if iid in core_ids) == pytest.approx(0.70)
    satellite = {iid: w for iid, w in weights.items() if iid not in core_ids}
    assert len(satellite) == 5
    assert all(w == pytest.approx(0.06) for w in satellite.values())


def test_core_satellite_without_core_leaves_the_rest_in_cash():
    stocks = [_cand(10 + i) for i in range(5)]
    features = {c.instrument_id: _momentum_feats() for c in stocks}
    weights = CoreSatelliteAllocator().allocate(stocks, features)
    assert sum(weights.values()) == pytest.approx(0.30)


class _FakeMood:
    def __init__(self, z: dict[str, float]):
        self.z = z
        self.calls: list[tuple[list[str], date]] = []

    def __call__(self, symbols: list[str], day: date) -> dict[str, float]:
        self.calls.append((symbols, day))
        return {s: v for s, v in self.z.items() if s in symbols}


def test_mood_tilt_without_mood_is_recent_momentum():
    candidates = [_cand(i) for i in range(1, 20)]
    features = {i: _momentum_feats(vol=0.1 + i / 100) for i in range(1, 20)}
    tilt = MoodTiltAllocator(_FakeMood({}))
    tilt.asof(date(2026, 1, 5))
    assert tilt.allocate(candidates, features) == RecentMomentumAllocator().allocate(
        candidates, features
    )


def test_mood_tilt_uses_the_engine_date_and_skips_funds_in_the_lookup():
    mood = _FakeMood({})
    tilt = MoodTiltAllocator(mood)
    tilt.asof(date(2026, 3, 2))
    tilt.allocate([_cand(1, "XEQT"), _cand(2, "RY")], {})
    assert mood.calls == [(["RY"], date(2026, 3, 2))]


def test_mood_tilt_promotes_warm_names_and_drops_the_coldest():
    candidates = [_cand(1, "AAA"), _cand(2, "BBB"), _cand(3, "CCC")]
    features = {1: _momentum_feats(vol=0.20), 2: _momentum_feats(vol=0.21), 3: _momentum_feats()}
    tilt = MoodTiltAllocator(_FakeMood({"AAA": -1.0, "BBB": 1.5, "CCC": -2.0}))
    tilt.max_names = 1
    tilt.asof(date(2026, 1, 5))
    # BBB's slightly weaker momentum is outweighed by its warm mood
    assert set(tilt.allocate(candidates, features)) == {2}
    scores = tilt.scores(candidates, features)
    assert 3 not in scores  # z below -1.5: excluded outright


def test_mood_tilt_without_asof_is_neutral():
    tilt = MoodTiltAllocator(_FakeMood({"AAA": 2.0}))
    weights = tilt.allocate([_cand(1, "AAA")], {1: _momentum_feats()})
    assert set(weights) == {1}
