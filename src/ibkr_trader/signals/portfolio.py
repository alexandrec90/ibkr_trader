"""Portfolio allocators — the decision layer.

Where a ``Predictor`` emits one signed score per instrument, an ``Allocator`` makes the actual
long-term decision: **target portfolio weights** across the eligible universe at a rebalance
date. Registered-account constraints are baked into every allocator's output contract:

- long-only (every weight ≥ 0 — no shorting in registered accounts),
- fully invested at most (weights sum to ≤ 1; the remainder is cash),
- a per-name cap so no single holding dominates (concentration = risk).

Allocators self-register by ``name`` (same pattern as signals.predictor) so the CLI/backtester
resolve one from a string, and the leaderboard ranks them head-to-head. ``ScoreAllocator`` is the
bridge that turns any registered ``Predictor`` into an allocator, so score-based models plug
straight into the portfolio backtester.

Allocators are pure: features in, weights out. They never touch the DB or the network — the
engine computes features (from bars ≤ t, no look-ahead) and hands them in.
"""

import abc
import math
from collections.abc import Sequence

from ibkr_trader.signals.eligibility import Candidate
from ibkr_trader.signals.predictor import Predictor, get_predictor

# feature dicts are keyed by instrument_id; values are per-instrument feature maps
Features = dict[int, dict[str, float]]
Weights = dict[int, float]


def normalize_weights(scores: dict[int, float], *, max_names: int, max_weight: float) -> Weights:
    """Turn non-negative conviction scores into capped, long-only target weights.

    Keeps the ``max_names`` highest scores, drops non-positive ones, caps each at
    ``max_weight``, and normalizes so the total is ≤ 1 (leftover is cash). Ties broken by
    instrument_id for determinism. A score set that is all-zero/negative → all cash.
    """
    positive = {iid: s for iid, s in scores.items() if s > 0}
    if not positive:
        return {}
    ranked = sorted(positive.items(), key=lambda kv: (-kv[1], kv[0]))[:max_names]
    total = sum(score for _, score in ranked)
    weights = {iid: min(max_weight, score / total) for iid, score in ranked}
    # capping can leave the book under-allocated; that headroom simply stays in cash.
    invested = sum(weights.values())
    if invested > 1.0:  # only possible via float wobble; renormalize down, never up
        weights = {iid: w / invested for iid, w in weights.items()}
    return weights


class Allocator(abc.ABC):
    name: str = "override-me"
    version: str = "0"
    max_names: int = 20
    max_weight: float = 0.25  # concentration cap per holding

    @abc.abstractmethod
    def allocate(self, candidates: Sequence[Candidate], features: Features) -> Weights:
        """Return target weights {instrument_id: weight}, long-only, summing to ≤ 1."""


REGISTRY: dict[str, type[Allocator]] = {}


def register(cls: type[Allocator]) -> type[Allocator]:
    """Class decorator: index an Allocator under its ``name`` for name-based resolution."""
    key = cls.name
    if key in REGISTRY and REGISTRY[key] is not cls:
        raise ValueError(f"duplicate allocator name {key!r}: {REGISTRY[key]!r} vs {cls!r}")
    REGISTRY[key] = cls
    return cls


def available() -> list[str]:
    """Registered allocator names, sorted — for CLI help and error messages."""
    return sorted(REGISTRY)


def get_allocator(name: str) -> Allocator:
    """Instantiate a registered allocator by name, or raise KeyError listing known names."""
    try:
        cls = REGISTRY[name]
    except KeyError:
        known = ", ".join(available()) or "(none registered)"
        raise KeyError(f"unknown allocator {name!r}; available: {known}") from None
    return cls()


@register
class EqualWeightAllocator(Allocator):
    """Equal weight across all eligible names (capped by ``max_names``). Honest, dumb baseline.

    15 names, same book size as ``momentum_lt``/``ml_lt`` so the leaderboard compares like
    with like. It must not be 20: a 1/20 = 5% target equals the default 5% rebalance band
    exactly, and the engine only trades drifts *past* the band — the baseline would never buy.
    """

    name = "equal_weight"
    version = "2"
    max_names = 15

    def allocate(self, candidates: Sequence[Candidate], features: Features) -> Weights:
        chosen = sorted(candidates, key=lambda c: c.instrument_id)[: self.max_names]
        if not chosen:
            return {}
        weight = min(self.max_weight, 1.0 / len(chosen))
        return {c.instrument_id: weight for c in chosen}


@register
class BuyAndHoldAllocator(Allocator):
    """100% into a single broad benchmark (default XEQT); the do-nothing long-term default.

    Falls back to equal-weight if the benchmark symbol isn't in the eligible set, so the run
    still produces something rather than sitting in cash.
    """

    name = "buy_and_hold"
    version = "1"
    benchmark_symbol = "XEQT"
    max_weight = 1.0

    def allocate(self, candidates: Sequence[Candidate], features: Features) -> Weights:
        for candidate in candidates:
            if candidate.symbol.upper() == self.benchmark_symbol.upper():
                return {candidate.instrument_id: 1.0}
        return EqualWeightAllocator().allocate(candidates, features)


@register
class MomentumTiltAllocator(Allocator):
    """Low-turnover long-term tilt: hold the top-momentum eligible names, inverse-vol weighted.

    A legitimate factor strategy that needs only price-derived features (``return_12m`` and
    ``volatility``, computed by the engine from bars ≤ t): rank by 12-month total return, keep
    the positive-momentum leaders, and size each inversely to its volatility so the book leans
    on steadier names (lower risk = higher weight). Missing features → that name is skipped.
    """

    name = "momentum_lt"
    version = "1"
    max_names = 15
    max_weight = 0.20

    def allocate(self, candidates: Sequence[Candidate], features: Features) -> Weights:
        scores: dict[int, float] = {}
        for candidate in candidates:
            feats = features.get(candidate.instrument_id, {})
            momentum = feats.get("return_12m")
            volatility = feats.get("volatility")
            if momentum is None or momentum <= 0:
                continue  # only hold names in a long-term uptrend
            # inverse-vol tilt; guard against zero/NaN vol with a floor.
            vol = volatility if (volatility and volatility > 0) else 1.0
            score = momentum / vol
            if math.isfinite(score):
                scores[candidate.instrument_id] = score
        return normalize_weights(scores, max_names=self.max_names, max_weight=self.max_weight)


class ScoreAllocator(Allocator):
    """Adapter: turn any registered ``Predictor`` into a portfolio allocator.

    Runs the predictor over each eligible candidate's features and converts positive signed
    scores into capped top-K weights. This is how a trained model (short- or long-term) plugs
    into the portfolio backtester without the engine knowing which model produced the signal.
    Not registered (it needs a predictor name); build it explicitly.
    """

    version = "1"

    def __init__(
        self,
        predictor: str | Predictor,
        *,
        max_names: int = 15,
        max_weight: float = 0.20,
    ):
        self._predictor = get_predictor(predictor) if isinstance(predictor, str) else predictor
        self.name = f"score:{self._predictor.name}"
        self.version = self._predictor.version
        self.max_names = max_names
        self.max_weight = max_weight

    def allocate(self, candidates: Sequence[Candidate], features: Features) -> Weights:
        scores: dict[int, float] = {}
        for candidate in candidates:
            score = self._predictor.predict(features.get(candidate.instrument_id, {}))
            if score > 0 and math.isfinite(score):
                scores[candidate.instrument_id] = score
        return normalize_weights(scores, max_names=self.max_names, max_weight=self.max_weight)


@register
class MlLtAllocator(ScoreAllocator):
    """The trained ``ml_lt`` predictor as a first-class strategy, resolvable by name.

    Thin registered wrapper so ``backtest run --strategy ml_lt`` works like any other
    allocator. Same concentration discipline as ``momentum_lt`` (top 15, 20% cap).
    Constructing it resolves the newest trained artifact (see signals.predictor.MlLongTerm)
    — no artifact or no ``[ml]`` extra raises there with a clear message.
    """

    name = "ml_lt"

    def __init__(self):
        super().__init__("ml_lt", max_names=15, max_weight=0.20)
        # ScoreAllocator renames itself "score:ml_lt"; keep the registry name so
        # backtest_runs.strategy matches --strategy ml_lt. version stays the artifact's
        # (e.g. "v1") — the engine pins it into params as model_version.
        self.name = MlLtAllocator.name
