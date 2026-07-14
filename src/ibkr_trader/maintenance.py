"""DB maintenance jobs — currently raw-payload pruning to reclaim disk.

Ingestion stores a trimmed ``raw`` JSON blob per news/social row so signals work has the
provider fields it might need. Once ``signals/`` has written a ``sentiment`` score, that blob has
done its job: this module NULLs it out to keep the database small (this box has little free
disk). Pruning is idempotent and only ever touches rows that are already scored — it never
deletes a row and never touches an unscored one.
"""

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import CursorResult, null, update
from sqlalchemy.orm import Session

from ibkr_trader.db.models import NewsArticle, SocialPost

#: Tables carrying a droppable ``raw`` blob alongside a ``sentiment`` score + ``fetched_at``.
_PRUNABLE = (NewsArticle, SocialPost)


def prune_scored_raw(session: Session, *, min_age_days: int = 0) -> dict[str, int]:
    """Drop ``raw`` on rows that have been sentiment-scored. Returns rows pruned per table.

    A row is pruned only when ``sentiment IS NOT NULL`` (signals has consumed it) and ``raw``
    is still populated. ``min_age_days`` is a safety grace period measured on ``fetched_at``:
    with the default 0 a row becomes prunable the moment it is scored; raise it to keep raw
    around for a while after scoring (e.g. for debugging).
    """
    if min_age_days < 0:
        raise ValueError("min_age_days must be >= 0")
    cutoff = datetime.now(UTC) - timedelta(days=min_age_days)

    counts: dict[str, int] = {}
    for model in _PRUNABLE:
        result = cast(
            CursorResult,
            session.execute(
                update(model)
                .where(
                    model.sentiment.is_not(None),
                    model.raw.is_not(None),
                    model.fetched_at <= cutoff,
                )
                # ``null()`` forces a real SQL NULL — plain None on a JSON column stores JSON
                # 'null' (SQLAlchemy none_as_null=False), which neither reclaims disk nor makes
                # IS NOT NULL false.
                .values(raw=null())
            ),
        )
        counts[model.__tablename__] = result.rowcount or 0
    return counts
