"""Model interface + registry. Keep it boring: features in, signed score out, persisted to
`predictions` with the feature snapshot for auditability.

Models self-register via ``@register`` so the CLI and backtester can resolve one from just a
name — the string stored in ``predictions.model_name`` / ``backtest_runs.strategy``. This lets
long-term and short-term models coexist and be ranked head-to-head (see backtest.compare).

Two contracts keep the registry boring:
- Registration is an import side-effect: a Predictor is only resolvable once its module has been
  imported. Keep concrete predictors in this module (or import them here) until that grows
  unwieldy, then switch to a proper entry-point/plugin scan.
- Registered predictors must be constructible with no arguments — a model that needs a trained
  artifact resolves its own path (e.g. by name+version) in ``__init__``/``load`` — so callers can
  build one from just its name.
"""

import abc


class Predictor(abc.ABC):
    name: str = "override-me"
    version: str = "0"

    @abc.abstractmethod
    def predict(self, features: dict) -> float:
        """Return a signed score (+ long / - short / 0 flat) for one instrument+horizon."""


REGISTRY: dict[str, type[Predictor]] = {}


def register(cls: type[Predictor]) -> type[Predictor]:
    """Class decorator: index a Predictor under its ``name`` so ``get_predictor`` can find it."""
    key = cls.name
    if key in REGISTRY and REGISTRY[key] is not cls:
        raise ValueError(f"duplicate predictor name {key!r}: {REGISTRY[key]!r} vs {cls!r}")
    REGISTRY[key] = cls
    return cls


def available() -> list[str]:
    """Registered predictor names, sorted — for CLI help and error messages."""
    return sorted(REGISTRY)


def get_predictor(name: str) -> Predictor:
    """Instantiate a registered predictor by name, or raise KeyError listing the known names."""
    try:
        cls = REGISTRY[name]
    except KeyError:
        known = ", ".join(available()) or "(none registered)"
        raise KeyError(f"unknown predictor {name!r}; available: {known}") from None
    return cls()


@register
class MomentumBaseline(Predictor):
    """Trivial baseline so backtests have something honest to compare against."""

    name = "momentum_baseline"
    version = "1"

    def predict(self, features: dict) -> float:
        # TODO(skeleton): sign of trailing 20d return, once build_daily_features() lands.
        raise NotImplementedError
