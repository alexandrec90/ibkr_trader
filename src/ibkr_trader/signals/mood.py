"""Public-mood panel: recent news + social sentiment per symbol, for the ``mood_tilt`` strategy.

Same pure-core + thin-DB-wrapper split as signals.features: ``MoodPanel`` answers "how has
the public been talking about these names lately?" from in-memory daily aggregates, and
``load_mood_panel`` builds it from the already-scored ``news_articles`` / ``social_posts``
rows (``signals.sentiment`` does the scoring; nothing here touches the network).

Recency is the point: each day's sentiment is weighted by an exponential decay (default
half-life 30 days) inside a 90-day window, so last week's headlines outweigh last quarter's.
No look-ahead: a decision on day ``t`` sees only items published on days strictly before
``t`` (``published_at`` is UTC, so a same-day article may land after the close).

Social posts count at half the weight of a news article by default — the social sample is
small and noisier. Authors never enter this module; only ``symbols``, ``sentiment`` and the
timestamp are read.
"""

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ibkr_trader.db.models import NewsArticle, SocialPost

#: Fewer than this many names with coverage on a day → no cross-section to z-score against.
#: High enough that a handful of sparse early social posts can't masquerade as a signal.
MIN_NAMES_FOR_Z = 10


@dataclass
class MoodPanel:
    """Daily sentiment aggregates per symbol and the decayed, z-scored lookup over them.

    ``daily[symbol]`` is sorted ``(day, weighted_sentiment_sum, weight)`` rows. Calling the
    panel is a ``signals.portfolio.MoodLookup``.
    """

    daily: dict[str, list[tuple[date, float, float]]] = field(default_factory=dict)
    window_days: int = 90
    half_life_days: float = 30.0
    min_coverage: float = 5.0  # effective items (news = 1, social = social_weight)

    def raw(self, symbol: str, day: date) -> tuple[float, float] | None:
        """(decayed mean sentiment, raw coverage) over the window before ``day``; None if empty."""
        rows = self.daily.get(symbol.upper())
        if not rows:
            return None
        days = [row[0] for row in rows]
        lo = bisect_left(days, day - timedelta(days=self.window_days))
        hi = bisect_left(days, day)  # strictly before the decision day
        num = den = coverage = 0.0
        for row_day, total, weight in rows[lo:hi]:
            decay = 0.5 ** ((day - row_day).days / self.half_life_days)
            num += decay * total
            den += decay * weight
            coverage += weight
        if den <= 0:
            return None
        return num / den, coverage

    def __call__(self, symbols: list[str], day: date) -> dict[str, float]:
        """Cross-sectional z-score of decayed mood among ``symbols`` with enough coverage."""
        means: dict[str, float] = {}
        for symbol in symbols:
            raw = self.raw(symbol, day)
            if raw is not None and raw[1] >= self.min_coverage:
                means[symbol.upper()] = raw[0]
        if len(means) < MIN_NAMES_FOR_Z:
            return {}
        values = np.asarray(list(means.values()), dtype=float)
        std = float(values.std())
        if std < 1e-9:  # float noise on a flat cross-section must not become a ±1 signal
            return dict.fromkeys(means, 0.0)
        mean = float(values.mean())
        return {symbol: (value - mean) / std for symbol, value in means.items()}

    def first_day(self) -> date | None:
        """Roughly where ``mood_tilt`` can start to differ: the first day on which at least
        ``MIN_NAMES_FOR_Z`` symbols have any data (coverage thresholds may push it later)."""
        firsts = sorted(rows[0][0] for rows in self.daily.values() if rows)
        return firsts[MIN_NAMES_FOR_Z - 1] if len(firsts) >= MIN_NAMES_FOR_Z else None


def build_panel(
    items: Iterable[tuple[datetime, Sequence[str] | None, float | None, float]],
    **panel_kwargs,
) -> MoodPanel:
    """Aggregate ``(timestamp, symbols, sentiment, weight)`` items into a ``MoodPanel``.

    Unscored items (sentiment None) and items with no symbols are skipped; each symbol an
    item names receives the item's full sentiment at the item's weight.
    """
    sums: dict[str, dict[date, list[float]]] = defaultdict(dict)
    for ts, symbols, sentiment, weight in items:
        if sentiment is None or not symbols:
            continue
        day = ts.date()
        for symbol in {s.upper() for s in symbols}:
            cell = sums[symbol].setdefault(day, [0.0, 0.0])
            cell[0] += sentiment * weight
            cell[1] += weight
    daily = {
        symbol: [(day, cell[0], cell[1]) for day, cell in sorted(by_day.items())]
        for symbol, by_day in sums.items()
    }
    return MoodPanel(daily=daily, **panel_kwargs)


def load_mood_panel(
    session: Session, *, model: str = "vader", social_weight: float = 0.5, **panel_kwargs
) -> MoodPanel:
    """Build the panel from scored news + social rows of one sentiment ``model``.

    Mixing models would mix scales (VADER and FinBERT disagree on what 0.3 means), so rows
    scored by any other model are ignored.
    """

    def items():
        news = select(NewsArticle.published_at, NewsArticle.symbols, NewsArticle.sentiment).where(
            NewsArticle.sentiment.is_not(None), NewsArticle.sentiment_model == model
        )
        for ts, symbols, sentiment in session.execute(news):
            yield ts, symbols, sentiment, 1.0
        social = select(SocialPost.created_at, SocialPost.symbols, SocialPost.sentiment).where(
            SocialPost.sentiment.is_not(None), SocialPost.sentiment_model == model
        )
        for ts, symbols, sentiment in session.execute(social):
            yield ts, symbols, sentiment, social_weight

    return build_panel(items(), **panel_kwargs)
