"""Persist sentiment scores onto ingested news/social rows.

The bridge between ingestion and features: connectors store rows with ``sentiment IS NULL``;
this module scores them with ``score_sentiment`` (VADER) and writes the score back. Scoring a
row also unlocks ``maintenance.prune_scored_raw`` dropping its ``raw`` blob, so running this
regularly (the ``sentiment_score`` serve job) keeps disk usage flat. DB only — no network.
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ibkr_trader.db.models import NewsArticle, SocialPost
from ibkr_trader.signals.features import score_sentiment

#: Rows loaded per query while draining the unscored backlog — bounds memory during the
#: initial backfill (tens of thousands of rows) without meaningfully slowing steady state.
DEFAULT_BATCH_SIZE = 500


def _news_text(article: NewsArticle) -> str:
    return ". ".join(part for part in (article.title, article.summary) if part)


def _post_text(post: SocialPost) -> str:
    return ". ".join(part for part in (post.title, post.body) if part)


#: (model, row → scorable text). Title plus summary/body: headlines alone are too terse for
#: a lexicon model, full raw payloads are already trimmed at ingestion.
_SCORABLE: tuple[tuple[type[Any], Callable[[Any], str]], ...] = (
    (NewsArticle, _news_text),
    (SocialPost, _post_text),
)


def score_pending(session: Session, *, batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, int]:
    """Score every unscored news/social row. Returns rows scored per table name.

    Idempotent by construction: only ``sentiment IS NULL`` rows are touched, and every touched
    row gets a non-NULL score (genuinely neutral or empty text scores 0.0), so a row is scored
    exactly once and re-runs are no-ops. Batched so the backlog never loads whole.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")
    counts: dict[str, int] = {}
    for model, text_of in _SCORABLE:
        scored = 0
        while True:
            rows = session.scalars(
                select(model).where(model.sentiment.is_(None)).limit(batch_size)
            ).all()
            if not rows:
                break
            for row in rows:
                row.sentiment = score_sentiment(text_of(row))
            session.flush()
            scored += len(rows)
        counts[model.__tablename__] = scored
    return counts
