from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.db.models import Base, NewsArticle, SocialPost
from ibkr_trader.signals.features import score_sentiment
from ibkr_trader.signals.sentiment import score_pending


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _article(external_id: str, title: str, summary: str | None, sentiment=None) -> NewsArticle:
    now = datetime.now(UTC)
    return NewsArticle(
        source="finnhub",
        external_id=external_id,
        published_at=now,
        title=title,
        url="u",
        summary=summary,
        symbols=["AAPL"],
        sentiment=sentiment,
        raw=None,
        fetched_at=now,
    )


def _post(external_id: str, title: str | None, body: str | None) -> SocialPost:
    now = datetime.now(UTC)
    return SocialPost(
        platform="reddit",
        channel="stocks",
        external_id=external_id,
        created_at=now,
        author_hash="h",
        title=title,
        body=body,
        score=1,
        num_comments=0,
        symbols=None,
        sentiment=None,
        raw=None,
        fetched_at=now,
    )


class TestScoreSentiment:
    def test_positive_text_scores_positive(self):
        assert score_sentiment("Record profits: an amazing, excellent quarter") > 0.2

    def test_negative_text_scores_negative(self):
        assert score_sentiment("Fraud lawsuit disaster: terrible losses, bankruptcy risk") < -0.2

    def test_empty_and_blank_score_zero(self):
        assert score_sentiment("") == 0.0
        assert score_sentiment("   ") == 0.0

    def test_bounded(self):
        for text in ("wonderful amazing best", "horrible worst disaster", "the report was filed"):
            assert -1.0 <= score_sentiment(text) <= 1.0


class TestScorePending:
    def test_scores_only_unscored_rows_in_both_tables(self):
        session = _session()
        session.add_all(
            [
                _article("a1", "Record profits, excellent quarter", "Beats expectations"),
                _article("a2", "t", "s", sentiment=0.9),  # already scored — must not change
                _post("p1", "Terrible losses", "bankruptcy fears grow"),
            ]
        )
        session.commit()

        counts = score_pending(session)
        session.commit()

        assert counts == {"news_articles": 1, "social_posts": 1}
        scored = session.scalar(select(NewsArticle).where(NewsArticle.external_id == "a1"))
        untouched = session.scalar(select(NewsArticle).where(NewsArticle.external_id == "a2"))
        post = session.scalar(select(SocialPost).where(SocialPost.external_id == "p1"))
        assert scored.sentiment is not None and scored.sentiment > 0
        assert untouched.sentiment == 0.9
        assert post.sentiment is not None and post.sentiment < 0

    def test_idempotent_second_run_scores_nothing(self):
        session = _session()
        session.add(_article("a1", "great news", None))
        session.commit()

        assert score_pending(session)["news_articles"] == 1
        session.commit()
        assert score_pending(session) == {"news_articles": 0, "social_posts": 0}

    def test_empty_text_rows_still_get_marked_scored(self):
        # A row with no scorable text must score 0.0 (not stay NULL), or the job would
        # reprocess it forever and pruning would never touch it.
        session = _session()
        session.add(_post("p1", None, None))
        session.commit()

        assert score_pending(session)["social_posts"] == 1
        session.commit()
        post = session.scalar(select(SocialPost).where(SocialPost.external_id == "p1"))
        assert post.sentiment == 0.0

    def test_batching_drains_backlog_larger_than_one_batch(self):
        session = _session()
        session.add_all([_article(f"a{i}", f"headline {i} is excellent", None) for i in range(5)])
        session.commit()

        counts = score_pending(session, batch_size=2)
        session.commit()

        assert counts["news_articles"] == 5
        remaining = session.scalars(
            select(NewsArticle).where(NewsArticle.sentiment.is_(None))
        ).all()
        assert remaining == []

    def test_rejects_nonpositive_batch_size(self):
        with pytest.raises(ValueError, match="batch_size"):
            score_pending(_session(), batch_size=0)
