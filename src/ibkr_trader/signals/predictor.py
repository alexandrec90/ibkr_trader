"""Model interface. Keep it boring: features in, signed score out, persisted to
`predictions` with the feature snapshot for auditability."""

import abc


class Predictor(abc.ABC):
    name: str = "override-me"
    version: str = "0"

    @abc.abstractmethod
    def predict(self, features: dict) -> float:
        """Return a signed score (+ long / - short / 0 flat) for one instrument+horizon."""


class MomentumBaseline(Predictor):
    """Trivial baseline so backtests have something honest to compare against."""

    name = "momentum_baseline"
    version = "1"

    def predict(self, features: dict) -> float:
        # TODO: e.g. sign of trailing 20d return. Placeholder until features exist.
        raise NotImplementedError
