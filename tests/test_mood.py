"""Public-mood panel: aggregation, decay, no look-ahead, z-scoring, and the DB loader."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.db.models import Base, NewsArticle, SocialPost
from ibkr_trader.signals import mood as mood_mod
from ibkr_trader.signals.mood import MoodPanel, build_panel, load_mood_panel

DAY = date(2026, 3, 2)


def _ts(day: date, hour: int = 15) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def test_build_panel_aggregates_per_symbol_and_day_and_skips_unusable_items():
    items = [
        (_ts(DAY), ["aapl", "MSFT"], 0.5, 1.0),
        (_ts(DAY, 18), ["AAPL"], -0.1, 0.5),
        (_ts(DAY), ["AAPL", "AAPL"], 0.2, 1.0),  # duplicate symbol counts once
        (_ts(DAY), ["AAPL"], None, 1.0),  # unscored
        (_ts(DAY), [], 0.9, 1.0),  # no symbols
        (_ts(DAY), None, 0.9, 1.0),
    ]
    panel = build_panel(items)
    day, total, weight = panel.daily["AAPL"][0]
    assert day == DAY
    assert total == pytest.approx(0.5 - 0.05 + 0.2)
    assert weight == pytest.approx(2.5)
    assert panel.daily["MSFT"] == [(DAY, 0.5, 1.0)]


def test_raw_only_sees_days_strictly_before_the_decision():
    panel = build_panel(
        [(_ts(DAY - timedelta(days=1)), ["A"], 0.4, 1.0), (_ts(DAY), ["A"], -0.9, 1.0)]
    )
    mean, coverage = panel.raw("A", DAY)
    # the same-day -0.9 may have been published after the close: invisible on DAY
    assert mean == pytest.approx(0.4)
    assert coverage == pytest.approx(1.0)
    assert panel.raw("A", DAY - timedelta(days=1)) is None
    assert panel.raw("NOPE", DAY) is None


def test_raw_decays_older_days_and_ignores_the_window_edge():
    panel = build_panel(
        [
            (_ts(DAY - timedelta(days=1)), ["A"], 1.0, 1.0),
            (_ts(DAY - timedelta(days=31)), ["A"], -1.0, 1.0),
            (_ts(DAY - timedelta(days=200)), ["A"], -1.0, 50.0),  # outside the 90-day window
        ]
    )
    mean, coverage = panel.raw("A", DAY)
    recent, old = 0.5 ** (1 / 30), 0.5 ** (31 / 30)
    assert mean == pytest.approx((recent - old) / (recent + old))
    assert mean > 0  # yesterday outweighs last month
    assert coverage == pytest.approx(2.0)


def _panel_with(means: dict[str, float], count: float = 10.0) -> MoodPanel:
    yesterday = DAY - timedelta(days=1)
    return MoodPanel(daily={s: [(yesterday, m * count, count)] for s, m in means.items()})


def test_call_z_scores_the_cross_section_of_covered_names():
    means = {f"S{i}": i / 10 for i in range(12)}
    panel = _panel_with(means)
    z = panel(list(means), DAY)
    assert set(z) == set(means)
    assert sum(z.values()) == pytest.approx(0.0, abs=1e-9)
    assert z["S11"] > 0 > z["S0"]


def test_call_needs_coverage_and_a_real_cross_section():
    means = {f"S{i}": i / 10 for i in range(12)}
    thin = _panel_with(means, count=1.0)  # below min_coverage
    assert thin(list(means), DAY) == {}
    few = _panel_with({f"S{i}": i / 10 for i in range(mood_mod.MIN_NAMES_FOR_Z - 1)})
    assert few([f"S{i}" for i in range(12)], DAY) == {}


def test_call_flat_cross_section_is_all_neutral():
    means = {f"S{i}": 0.2 for i in range(12)}
    assert set(_panel_with(means)(list(means), DAY).values()) == {0.0}


def test_first_day_is_when_enough_names_have_data():
    daily = {f"S{i}": [(DAY + timedelta(days=i), 0.0, 1.0)] for i in range(12)}
    panel = MoodPanel(daily=daily)
    assert panel.first_day() == DAY + timedelta(days=mood_mod.MIN_NAMES_FOR_Z - 1)
    assert MoodPanel(daily={"A": [(DAY, 0.0, 1.0)]}).first_day() is None


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_load_mood_panel_reads_one_model_and_halves_social_weight():
    session = _session()
    ts = _ts(DAY)
    for i, (sentiment, model) in enumerate([(0.6, "vader"), (0.9, "finbert"), (None, None)]):
        session.add(
            NewsArticle(
                source="finnhub",
                external_id=f"n{i}",
                published_at=ts,
                title="t",
                url="u",
                summary=None,
                symbols=["AAPL"],
                sentiment=sentiment,
                sentiment_model=model,
                raw=None,
                fetched_at=ts,
            )
        )
    session.add(
        SocialPost(
            platform="reddit",
            channel="stocks",
            external_id="p1",
            created_at=ts,
            author_hash="h",
            title="t",
            body=None,
            score=1,
            num_comments=0,
            symbols=["AAPL"],
            sentiment=-0.2,
            sentiment_model="vader",
            raw=None,
            fetched_at=ts,
        )
    )
    session.flush()
    panel = load_mood_panel(session, model="vader")
    assert len(panel.daily["AAPL"]) == 1
    _, total, weight = panel.daily["AAPL"][0]
    assert weight == pytest.approx(1.5)  # one vader article + one half-weight post
    assert total == pytest.approx(0.6 - 0.1)
