from ibkr_trader.signals.eligibility import Candidate
from ibkr_trader.signals.portfolio import (
    BuyAndHoldAllocator,
    EqualWeightAllocator,
    MomentumTiltAllocator,
    ScoreAllocator,
    available,
    get_allocator,
    normalize_weights,
)
from ibkr_trader.signals.predictor import Predictor


def _cand(iid: int, symbol: str = "") -> Candidate:
    return Candidate(
        instrument_id=iid,
        symbol=symbol or f"S{iid}",
        currency="CAD",
        exchange="TSX",
        last_price=50.0,
        avg_dollar_volume=5_000_000.0,
        history_days=1000,
    )


def test_registry_resolves_by_name():
    assert "momentum_lt" in available()
    assert isinstance(get_allocator("equal_weight"), EqualWeightAllocator)


def test_normalize_weights_is_long_only_capped_and_topk():
    weights = normalize_weights({1: 10.0, 2: 5.0, 3: 1.0, 4: -2.0}, max_names=2, max_weight=0.6)
    assert set(weights) == {1, 2}  # top-2, negative dropped
    assert all(w >= 0 for w in weights.values())  # long-only
    assert all(w <= 0.6 + 1e-9 for w in weights.values())  # cap respected
    assert sum(weights.values()) <= 1.0 + 1e-9


def test_normalize_weights_all_nonpositive_is_all_cash():
    assert normalize_weights({1: 0.0, 2: -1.0}, max_names=5, max_weight=1.0) == {}


def test_equal_weight_allocator():
    weights = EqualWeightAllocator().allocate([_cand(1), _cand(2), _cand(3), _cand(4)], {})
    assert len(weights) == 4
    assert all(abs(w - 0.25) < 1e-9 for w in weights.values())


def test_momentum_tilt_holds_only_uptrends_and_prefers_low_vol():
    candidates = [_cand(1), _cand(2), _cand(3)]
    features = {
        1: {"return_12m": 0.20, "volatility": 0.10},  # up, calm → biggest weight
        2: {"return_12m": 0.20, "volatility": 0.40},  # up, choppy → smaller weight
        3: {"return_12m": -0.10, "volatility": 0.10},  # downtrend → excluded
    }
    allocator = MomentumTiltAllocator()
    allocator.max_weight = 1.0  # lift the concentration cap so the tilt is observable
    weights = allocator.allocate(candidates, features)
    assert set(weights) == {1, 2}
    assert weights[1] > weights[2]  # inverse-vol tilt: calmer name gets more


def test_buy_and_hold_picks_benchmark_else_equal_weight():
    with_bench = BuyAndHoldAllocator().allocate([_cand(1), _cand(2, "XEQT")], {})
    assert with_bench == {2: 1.0}
    without = BuyAndHoldAllocator().allocate([_cand(1), _cand(2)], {})
    assert len(without) == 2  # falls back to equal weight


def test_score_allocator_wraps_a_predictor():
    class FeaturePredictor(Predictor):
        name = "feat_test"
        version = "9"

        def predict(self, features: dict) -> float:
            return features.get("edge", 0.0)

    allocator = ScoreAllocator(FeaturePredictor(), max_names=5, max_weight=1.0)
    assert allocator.name == "score:feat_test"
    weights = allocator.allocate(
        [_cand(1), _cand(2), _cand(3)],
        {1: {"edge": 2.0}, 2: {"edge": 1.0}, 3: {"edge": -1.0}},
    )
    assert set(weights) == {1, 2}  # only positive scores held
    assert weights[1] > weights[2]
