import pytest

from ibkr_trader.signals.predictor import (
    MomentumBaseline,
    Predictor,
    available,
    get_predictor,
    register,
)


def test_momentum_baseline_is_registered():
    assert "momentum_baseline" in available()
    predictor = get_predictor("momentum_baseline")
    assert isinstance(predictor, MomentumBaseline)
    assert predictor.name == "momentum_baseline"


def test_get_predictor_unknown_lists_available():
    with pytest.raises(KeyError) as exc_info:
        get_predictor("does_not_exist")
    assert "momentum_baseline" in str(exc_info.value)


def test_register_rejects_a_clashing_name():
    with pytest.raises(ValueError, match="duplicate predictor name"):

        @register
        class Clashing(Predictor):
            name = "momentum_baseline"

            def predict(self, features: dict) -> float:
                return 0.0
