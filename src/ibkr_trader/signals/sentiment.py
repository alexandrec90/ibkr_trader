"""Persist configured sentiment scores and model provenance on ingested text rows.

The scheduled path is DB-only: FinBERT loads strictly from the local Hugging Face cache, while
model downloading is an explicit CLI operation. A score unlocks raw-payload pruning; the
surviving title/summary/body permits later re-scoring with a better model.
"""

from collections.abc import Callable
from typing import Any, Literal, Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ibkr_trader.config import get_settings
from ibkr_trader.db.models import NewsArticle, SocialPost
from ibkr_trader.signals.features import score_sentiment

DEFAULT_BATCH_SIZE = 500
SentimentModel = Literal["vader", "finbert"]


class SentimentScorer(Protocol):
    model_name: str

    def score_many(self, texts: list[str], *, batch_size: int) -> list[float]: ...


class VaderScorer:
    """Batch-shaped adapter around the existing lazy VADER scorer."""

    model_name = "vader"

    def score_many(self, texts: list[str], *, batch_size: int) -> list[float]:
        return [score_sentiment(text) for text in texts]


def scorer_for_model(model: SentimentModel) -> SentimentScorer:
    """Build a scorer without importing the optional FinBERT stack unless selected."""
    if model == "vader":
        return VaderScorer()
    if model == "finbert":
        from ibkr_trader.signals.finbert import FinbertScorer

        return FinbertScorer()
    raise ValueError(f"unknown sentiment model: {model}")


def _news_text(article: NewsArticle) -> str:
    return ". ".join(part for part in (article.title, article.summary) if part)


def _post_text(post: SocialPost) -> str:
    return ". ".join(part for part in (post.title, post.body) if part)


_SCORABLE: tuple[tuple[type[Any], Callable[[Any], str]], ...] = (
    (NewsArticle, _news_text),
    (SocialPost, _post_text),
)


def score_pending(
    session: Session,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    model: SentimentModel | None = None,
) -> dict[str, int]:
    """Score every row with ``sentiment IS NULL`` using the configured local scorer."""
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")
    scorer = scorer_for_model(model or get_settings().sentiment_model)
    counts: dict[str, int] = {}
    for row_model, text_of in _SCORABLE:
        scored = 0
        while True:
            rows = session.scalars(
                select(row_model).where(row_model.sentiment.is_(None)).limit(batch_size)
            ).all()
            if not rows:
                break
            scores = scorer.score_many([text_of(row) for row in rows], batch_size=batch_size)
            for row, score in zip(rows, scores, strict=True):
                row.sentiment = score
                row.sentiment_model = scorer.model_name
            session.flush()
            scored += len(rows)
        counts[row_model.__tablename__] = scored
    return counts


def rescore(
    session: Session,
    *,
    model: SentimentModel,
    limit: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    """Re-score rows not already stamped with ``model``; deterministic and idempotent."""
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be > 0")
    scorer = scorer_for_model(model)
    counts: dict[str, int] = {}
    remaining = limit
    for row_model, text_of in _SCORABLE:
        scored = 0
        while remaining is None or remaining > 0:
            take = batch_size if remaining is None else min(batch_size, remaining)
            rows = session.scalars(
                select(row_model)
                .where(
                    or_(
                        row_model.sentiment_model.is_(None),
                        row_model.sentiment_model != scorer.model_name,
                    )
                )
                .order_by(row_model.id)
                .limit(take)
            ).all()
            if not rows:
                break
            scores = scorer.score_many([text_of(row) for row in rows], batch_size=take)
            for row, score in zip(rows, scores, strict=True):
                row.sentiment = score
                row.sentiment_model = scorer.model_name
            session.flush()
            scored += len(rows)
            if remaining is not None:
                remaining -= len(rows)
        counts[row_model.__tablename__] = scored
    return counts
