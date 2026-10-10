from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.db.models import Base, NewsArticle, SocialPost
from ibkr_trader.maintenance import prune_scored_raw


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _article(external_id: str, sentiment, fetched_at: datetime) -> NewsArticle:
    return NewsArticle(
        source="finnhub",
        external_id=external_id,
        published_at=fetched_at,
        title="t",
        url="u",
        summary="s",
        symbols=["AAPL"],
        sentiment=sentiment,
        raw={"source": "Reuters", "category": "company"},
        fetched_at=fetched_at,
    )


def _post(external_id: str, sentiment, fetched_at: datetime) -> SocialPost:
    return SocialPost(
        platform="reddit",
        channel="stocks",
        external_id=external_id,
        created_at=fetched_at,
        author_hash="h",
        title="t",
        body="b",
        score=1,
        num_comments=0,
        symbols=None,
        sentiment=sentiment,
        raw={"permalink": "/r/x/1"},
        fetched_at=fetched_at,
    )


def test_prune_drops_raw_only_on_scored_rows():
    session = _session()
    now = datetime.now(UTC)
    session.add_all(
        [
            _article("scored", sentiment=0.4, fetched_at=now),
            _article("unscored", sentiment=None, fetched_at=now),
            _post("scored", sentiment=-0.2, fetched_at=now),
            _post("unscored", sentiment=None, fetched_at=now),
        ]
    )
    session.commit()

    counts = prune_scored_raw(session)
    session.commit()

    assert counts == {"news_articles": 1, "social_posts": 1}
    scored_article = session.scalar(select(NewsArticle).where(NewsArticle.external_id == "scored"))
    unscored_article = session.scalar(
        select(NewsArticle).where(NewsArticle.external_id == "unscored")
    )
    assert scored_article.raw is None  # reclaimed
    assert unscored_article.raw is not None  # left intact
    scored_post = session.scalar(select(SocialPost).where(SocialPost.external_id == "scored"))
    assert scored_post.raw is None


def test_prune_is_idempotent():
    session = _session()
    now = datetime.now(UTC)
    session.add(_article("scored", sentiment=0.4, fetched_at=now))
    session.commit()

    assert prune_scored_raw(session)["news_articles"] == 1
    session.commit()
    # already pruned -> nothing left to do (raw is NULL now)
    assert prune_scored_raw(session)["news_articles"] == 0


def test_prune_respects_min_age_grace():
    session = _session()
    now = datetime.now(UTC)
    session.add_all(
        [
            _article("fresh", sentiment=0.1, fetched_at=now),
            _article("old", sentiment=0.1, fetched_at=now - timedelta(days=10)),
        ]
    )
    session.commit()

    counts = prune_scored_raw(session, min_age_days=7)
    session.commit()

    assert counts["news_articles"] == 1  # only the 10-day-old row
    fresh = session.scalar(select(NewsArticle).where(NewsArticle.external_id == "fresh"))
    old = session.scalar(select(NewsArticle).where(NewsArticle.external_id == "old"))
    assert fresh.raw is not None
    assert old.raw is None


def test_prune_rejects_negative_age():
    session = _session()
    with pytest.raises(ValueError, match="min_age_days"):
        prune_scored_raw(session, min_age_days=-1)
    with pytest.raises(ValueError, match="batch_rows"):
        prune_scored_raw(session, batch_rows=0)


def test_prune_commits_in_bounded_batches(monkeypatch):
    """2026-10-10: one UPDATE over 8.1M scored social posts held their row locks for as long
    as it ran, deadlocked with the social poll and sentiment scoring, rolled back, and the
    job failed every day. A batch holds at most `batch_rows` rows' locks and commits."""
    session = _session()
    now = datetime.now(UTC)
    session.add_all([_post(f"p{i}", sentiment=0.1, fetched_at=now) for i in range(5)])
    session.add(_post("unscored", sentiment=None, fetched_at=now))
    session.commit()
    commits: list[None] = []
    real_commit = session.commit
    monkeypatch.setattr(session, "commit", lambda: (commits.append(None), real_commit())[1])

    counts = prune_scored_raw(session, batch_rows=2)

    assert counts == {"news_articles": 0, "social_posts": 5}
    assert len(commits) == 3, "five rows in batches of two, each committed"
    unscored = session.scalar(select(SocialPost).where(SocialPost.external_id == "unscored"))
    assert unscored.raw is not None


def test_a_prune_that_fails_partway_keeps_the_batches_it_committed(monkeypatch):
    session = _session()
    now = datetime.now(UTC)
    session.add_all([_post(f"p{i}", sentiment=0.1, fetched_at=now) for i in range(4)])
    session.commit()
    real_commit = session.commit
    commits: list[None] = []

    def commit_once_then_deadlock():
        if commits:
            raise RuntimeError("deadlock detected")
        commits.append(None)
        real_commit()

    monkeypatch.setattr(session, "commit", commit_once_then_deadlock)
    with pytest.raises(RuntimeError, match="deadlock"):
        prune_scored_raw(session, batch_rows=2)
    session.rollback()
    pruned = [p for p in session.scalars(select(SocialPost)) if p.raw is None]
    assert len(pruned) == 2, "the committed batch stays pruned; the next run does the rest"
