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
from collections.abc import Callable, Sequence
from datetime import date

from ibkr_trader.signals.eligibility import Candidate
from ibkr_trader.signals.predictor import Predictor, get_predictor

# feature dicts are keyed by instrument_id; values are per-instrument feature maps
Features = dict[int, dict[str, float]]
Weights = dict[int, float]
#: (symbols, decision day) -> {symbol: mood z-score}, symbols lacking coverage omitted.
MoodLookup = Callable[[list[str], date], dict[str, float]]


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

    def asof(self, day: date) -> None:  # noqa: B027 - deliberately optional, not abstract
        """Engine hook: the decision date, called right before each ``allocate``.

        Stateless allocators ignore it (this default). Date-aware ones — e.g. the per-fold
        OOS allocator that must pick the model trained strictly before ``day`` — override it;
        they still receive only bar-derived features computed from data ≤ day.
        """


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


# ---------------------------------------------------------------------------------------------
# Registered-account buy-and-hold strategies (docs/registered-account-strategy.md §Strategies).
#
# Shared discipline: long-only, equal-weighted (so drift, not re-weighting, is what triggers a
# trade), and a *rank buffer* — an incumbent is kept while it stays inside the top
# ``n × hold_buffer`` instead of being swapped the moment it slips out of the top ``n``. That
# buffer is what turns a ranking into a buy-and-hold book. Signals lean on the most recent
# quarter of price action rather than fundamentals (none are ingested, and the owner's view is
# that recent behaviour matters more than it used to).
# ---------------------------------------------------------------------------------------------

#: The broad-market ETFs in the curated universe — a mirror of ``tickers-etfs.txt`` (a test
#: keeps the two equal). ``instruments.asset_class`` is not populated by ingestion yet, so the
#: stock-picking strategies use this to tell a fund from a company.
BROAD_ETF_SYMBOLS: frozenset[str] = frozenset(
    "XEQT VEQT XGRO VGRO XBAL VBAL XIC XIU VCN VFV ZSP XUU XBB ZAG XSP XAW "
    "SPY VTI VOO QQQ IWM EFA EEM AGG".split()
)

#: An all-equity, XEQT-like mix (~25% Canada / 45% US / 25% developed ex-NA / 5% emerging)
#: built from ETFs with history back to 2003, so the do-nothing reference spans every window.
COUCH_POTATO_MIX: dict[str, float] = {"XIC": 0.25, "SPY": 0.45, "EFA": 0.25, "EEM": 0.05}

#: Weights on the latest quarter, the quarter before it, and the half-year before that. The
#: whole trailing year still votes, but per unit of time the latest quarter counts 5x the
#: oldest half-year.
RECENCY_WEIGHTS: tuple[float, float, float] = (0.5, 0.3, 0.2)


def is_fund(candidate: Candidate) -> bool:
    """True for an ETF/fund rather than an operating company."""
    return (candidate.asset_class or "").upper() == "ETF" or (
        candidate.symbol.upper() in BROAD_ETF_SYMBOLS
    )


def recency_momentum(feats: dict[str, float]) -> float | None:
    """Recency-weighted trailing-year return: last quarter, prior quarter, prior half-year.

    Decomposes the 3/6/12-month total returns into non-overlapping legs and weights them by
    ``RECENCY_WEIGHTS``. None when any of the three returns is missing (young listing).
    """
    r3, r6, r12 = feats.get("return_3m"), feats.get("return_6m"), feats.get("return_12m")
    if r3 is None or r6 is None or r12 is None or r3 <= -1 or r6 <= -1:
        return None
    last_quarter = r3
    prior_quarter = (1 + r6) / (1 + r3) - 1
    prior_half = (1 + r12) / (1 + r6) - 1
    w1, w2, w3 = RECENCY_WEIGHTS
    return w1 * last_quarter + w2 * prior_quarter + w3 * prior_half


def select_with_buffer(
    scores: dict[int, float], held: set[int], *, n: int, hold_buffer: float
) -> list[int]:
    """Top ``n`` by score, but incumbents ranked inside ``n × hold_buffer`` keep their seat.

    Deterministic (ties broken by instrument_id). Incumbents are seated first, best-ranked
    first, then the remaining seats go to the best newcomers.
    """
    ranked = sorted(scores, key=lambda iid: (-scores[iid], iid))
    keep_depth = max(n, int(n * hold_buffer))
    picks = [iid for iid in ranked[:keep_depth] if iid in held][:n]
    for iid in ranked:
        if len(picks) >= n:
            break
        if iid not in picks:
            picks.append(iid)
    return picks


class BufferedStockAllocator(Allocator):
    """Equal-weight top-``n`` stock picker with a rank buffer. Subclasses supply ``score``.

    Remembers what it chose last time (per instance), which is how the buffer knows the
    incumbents. A fresh instance starts with an empty book — build one per simulation.
    """

    max_names = 12
    hold_buffer = 2.0

    def __init__(self) -> None:
        self._held: set[int] = set()

    def score(self, candidate: Candidate, feats: dict[str, float]) -> float | None:
        raise NotImplementedError

    def scores(self, candidates: Sequence[Candidate], features: Features) -> dict[int, float]:
        """Positive, finite scores for the eligible operating companies (funds skipped)."""
        out: dict[int, float] = {}
        for candidate in candidates:
            if is_fund(candidate):
                continue
            value = self.score(candidate, features.get(candidate.instrument_id, {}))
            if value is not None and value > 0 and math.isfinite(value):
                out[candidate.instrument_id] = value
        return out

    def pick(self, scores: dict[int, float], n: int) -> list[int]:
        picks = select_with_buffer(scores, self._held, n=n, hold_buffer=self.hold_buffer)
        self._held = set(picks)
        return picks

    def allocate(self, candidates: Sequence[Candidate], features: Features) -> Weights:
        picks = self.pick(self.scores(candidates, features), self.max_names)
        if not picks:
            return {}
        weight = min(self.max_weight, 1.0 / len(picks))
        return {iid: weight for iid in picks}


@register
class RecentMomentumAllocator(BufferedStockAllocator):
    """Stocks in a strong, *recent*, orderly uptrend — held until they clearly fade.

    Score = recency-weighted momentum (``recency_momentum``) divided by the last 60 days'
    volatility, so a steady climb beats a jagged one. A name more than 20% below its 52-week
    high is skipped: the trend is broken, whatever the trailing year says. The wide buffer
    (an incumbent keeps its seat while inside the top 48) is what holds turnover to roughly
    two dozen trades a year at a semi-annual review — tuned for turnover, not for return.
    """

    name = "recent_momentum"
    version = "1"
    max_names = 12
    max_weight = 0.12
    hold_buffer = 4.0
    max_off_high = -0.20

    def score(self, candidate: Candidate, feats: dict[str, float]) -> float | None:
        momentum = recency_momentum(feats)
        vol = feats.get("volatility_60d")
        off_high = feats.get("pct_off_52w_high")
        if momentum is None or not vol or vol <= 0:
            return None
        if off_high is not None and off_high < self.max_off_high:
            return None
        return momentum / vol


@register
class SteadyCompoundersAllocator(BufferedStockAllocator):
    """The calmest stocks that are still compounding — a defensive, sleep-at-night book.

    Without fundamentals, "quality" is proxied by price behaviour: rank by low risk, where
    risk leans on the recent past (60% last-60-day volatility, 40% one-year downside
    deviation). Only names up over the year and with a one-year drawdown shallower than 30%
    qualify, so "calm because it is slowly dying" is screened out.
    """

    name = "steady_compounders"
    version = "1"
    max_names = 15
    max_weight = 0.10
    max_drawdown_floor = -0.30

    def score(self, candidate: Candidate, feats: dict[str, float]) -> float | None:
        r12 = feats.get("return_12m")
        drawdown = feats.get("max_drawdown_252d")
        vol = feats.get("volatility_60d")
        downside = feats.get("downside_deviation_252d")
        if r12 is None or r12 <= 0 or drawdown is None or drawdown < self.max_drawdown_floor:
            return None
        if vol is None or downside is None:
            return None
        risk = 0.6 * vol + 0.4 * downside
        return 1.0 / risk if risk > 0 else None


def _fixed_mix(candidates: Sequence[Candidate], mix: dict[str, float]) -> Weights:
    """``mix`` (symbol → weight) over the eligible candidates, renormalized over those present."""
    present = {c.symbol.upper(): c.instrument_id for c in candidates}
    available_mix = {sym: w for sym, w in mix.items() if sym in present}
    total = sum(available_mix.values())
    if total <= 0:
        return {}
    return {present[sym]: w / total for sym, w in available_mix.items()}


@register
class CouchPotatoAllocator(Allocator):
    """The do-nothing reference: a fixed XEQT-like ETF mix (``COUCH_POTATO_MIX``).

    If a sleeve's ETF is not yet eligible, the others are scaled up to stay fully invested.
    """

    name = "couch_potato"
    version = "1"
    max_weight = 1.0

    def allocate(self, candidates: Sequence[Candidate], features: Features) -> Weights:
        return _fixed_mix(candidates, COUCH_POTATO_MIX)


@register
class CoreSatelliteAllocator(Allocator):
    """70% couch-potato core, 30% in five ``recent_momentum`` stocks (6% each).

    The core keeps the account diversified and nearly turnover-free; the satellite is where
    stock selection gets a bounded say. If the core ETFs are unavailable the satellite still
    only gets its 30% — the rest waits in cash rather than concentrating.
    """

    name = "core_satellite"
    version = "1"
    core_share = 0.70
    satellite_names = 5

    def __init__(self) -> None:
        self._satellite = RecentMomentumAllocator()

    def allocate(self, candidates: Sequence[Candidate], features: Features) -> Weights:
        core = _fixed_mix(candidates, COUCH_POTATO_MIX)
        weights = {iid: w * self.core_share for iid, w in core.items()}
        scores = self._satellite.scores(candidates, features)
        picks = self._satellite.pick(scores, self.satellite_names)
        each = (1.0 - self.core_share) / self.satellite_names
        for iid in picks:
            weights[iid] = weights.get(iid, 0.0) + each
        return weights


class MoodTiltAllocator(RecentMomentumAllocator):
    """``recent_momentum`` tilted by recent public mood (news + social sentiment).

    ``mood`` is a pure ``MoodLookup`` supplied at build time (see ``signals.mood``), consulted
    with the date the engine passes to ``asof``. Each
    name's momentum score is scaled by ``1 + tilt × z`` (z clipped to ±2), and a name whose
    mood is worse than ``exclude_below`` standard deviations is dropped. Names with too little
    coverage are neutral (z = 0), so before any news exists this *is* ``recent_momentum`` —
    which makes the two directly comparable. Not registered: it needs the mood panel.
    """

    name = "mood_tilt"
    version = "1"
    tilt = 0.25
    exclude_below = -1.5

    def __init__(self, mood: MoodLookup) -> None:
        super().__init__()
        self._mood = mood
        self._day: date | None = None
        self._z: dict[str, float] = {}

    def asof(self, day: date) -> None:
        self._day = day

    def allocate(self, candidates: Sequence[Candidate], features: Features) -> Weights:
        symbols = [c.symbol.upper() for c in candidates if not is_fund(c)]
        self._z = self._mood(symbols, self._day) if self._day is not None else {}
        return super().allocate(candidates, features)

    def score(self, candidate: Candidate, feats: dict[str, float]) -> float | None:
        base = super().score(candidate, feats)
        if base is None:
            return None
        z = self._z.get(candidate.symbol.upper(), 0.0)
        if z < self.exclude_below:
            return None
        return base * (1.0 + self.tilt * max(-2.0, min(2.0, z)))


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


@register
class MlLtRidgeAllocator(ScoreAllocator):
    """The saved ridge floor as a top-15, 20%-cap first-class strategy."""

    name = "ml_lt_ridge"

    def __init__(self):
        super().__init__("ml_lt_ridge", max_names=15, max_weight=0.20)
        self.name = MlLtRidgeAllocator.name
