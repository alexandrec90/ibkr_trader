"""DB maintenance jobs — currently raw-payload pruning to reclaim disk.

Ingestion stores a trimmed ``raw`` JSON blob per news/social row so signals work has the
provider fields it might need. Once ``signals/`` has written a ``sentiment`` score, that blob has
done its job: this module NULLs it out to keep the database small (this box has little free
disk). Pruning is idempotent and only ever touches rows that are already scored — it never
deletes a row and never touches an unscored one.
"""

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import CursorResult, null, select, update
from sqlalchemy.orm import Session

from ibkr_trader.db.models import NewsArticle, SocialPost

#: Tables carrying a droppable ``raw`` blob alongside a ``sentiment`` score + ``fetched_at``.
_PRUNABLE = (NewsArticle, SocialPost)

#: Rows one committed prune batch NULLs, and so the most row locks it ever holds.
BATCH_ROWS = 20_000


def prune_scored_raw(
    session: Session, *, min_age_days: int = 0, batch_rows: int = BATCH_ROWS
) -> dict[str, int]:
    """Drop ``raw`` on rows that have been sentiment-scored. Returns rows pruned per table.

    A row is pruned only when ``sentiment IS NOT NULL`` (signals has consumed it) and ``raw``
    is still populated. ``min_age_days`` is a safety grace period measured on ``fetched_at``:
    with the default 0 a row becomes prunable the moment it is scored; raise it to keep raw
    around for a while after scoring (e.g. for debugging).

    **``batch_rows`` at a time, each batch committed.** One UPDATE over a bulk import's 8.1M
    social posts held all their row locks for as long as it ran, deadlocked with the social
    poll and sentiment scoring, rolled back, and failed the job every day. A batch holds a
    bounded set of locks for a moment, and a run that fails partway keeps what it committed.
    """
    if min_age_days < 0:
        raise ValueError("min_age_days must be >= 0")
    if batch_rows < 1:
        raise ValueError("batch_rows must be >= 1")
    cutoff = datetime.now(UTC) - timedelta(days=min_age_days)

    counts: dict[str, int] = {}
    for model in _PRUNABLE:
        pruned = 0
        after = 0
        while True:
            ids = list(
                session.scalars(
                    select(model.id)
                    .where(
                        model.sentiment.is_not(None),
                        model.raw.is_not(None),
                        model.fetched_at <= cutoff,
                        model.id > after,
                    )
                    .order_by(model.id)
                    .limit(batch_rows)
                )
            )
            if not ids:
                break
            result = cast(
                CursorResult,
                # ``null()`` forces a real SQL NULL — plain None on a JSON column stores JSON
                # 'null' (SQLAlchemy none_as_null=False), which neither reclaims disk nor makes
                # IS NOT NULL false.
                session.execute(update(model).where(model.id.in_(ids)).values(raw=null())),
            )
            session.commit()
            pruned += result.rowcount or 0
            after = ids[-1]
        counts[model.__tablename__] = pruned
    return counts
