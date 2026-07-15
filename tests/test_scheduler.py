from datetime import timedelta
from types import SimpleNamespace

from ibkr_trader import scheduler


def _settings(**overrides):
    base = dict(
        poll_reddit_minutes=30,
        poll_finnhub_news_hours=6,
        poll_trends_hours=24,
        prune_raw_hours=24,
        prune_raw_min_age_days=0,
        news_universe_file="tickers.txt",
        finnhub_request_spacing_seconds=1.1,
        trends_keywords=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_scheduler_registers_all_jobs_with_configured_intervals():
    sched = scheduler.build_scheduler(settings=_settings())
    jobs = {job.id: job for job in sched.get_jobs()}
    assert set(jobs) == {"reddit_poll", "finnhub_news_poll", "trends_poll", "prune_raw"}
    assert jobs["reddit_poll"].trigger.interval == timedelta(minutes=30)
    assert jobs["finnhub_news_poll"].trigger.interval == timedelta(hours=6)
    assert jobs["trends_poll"].trigger.interval == timedelta(hours=24)
    assert jobs["prune_raw"].trigger.interval == timedelta(hours=24)


def test_build_scheduler_honours_overridden_cadence():
    sched = scheduler.build_scheduler(settings=_settings(poll_reddit_minutes=5))
    job = {j.id: j for j in sched.get_jobs()}["reddit_poll"]
    assert job.trigger.interval == timedelta(minutes=5)


def test_poll_reddit_returns_connector_count(monkeypatch):
    from ibkr_trader.ingestion.social.reddit import RedditConnector

    monkeypatch.setattr(RedditConnector, "fetch", lambda self, **kw: 12)
    assert scheduler.poll_reddit() == 12


def test_poll_trends_noops_without_keywords():
    assert scheduler.poll_trends([]) == 0


def test_poll_trends_calls_connector_with_keywords(monkeypatch):
    from ibkr_trader.ingestion.social.google_trends import GoogleTrendsConnector

    seen = {}

    def fake_fetch(self, keywords=None, **kw):
        seen["keywords"] = keywords
        return len(keywords)

    monkeypatch.setattr(GoogleTrendsConnector, "fetch", fake_fetch)
    assert scheduler.poll_trends(["AAPL", "Tesla"]) == 2
    assert seen["keywords"] == ["AAPL", "Tesla"]


def test_poll_finnhub_news_iterates_universe_and_spaces_calls(monkeypatch, tmp_path):
    universe = tmp_path / "u.txt"
    universe.write_text("aapl\nmsft\ngoog\n")
    from ibkr_trader.ingestion.news.finnhub_news import FinnhubNewsConnector

    calls: list[str] = []
    sleeps: list[float] = []

    def fake_fetch(self, symbol="", **kw):
        calls.append(symbol)
        return 2

    monkeypatch.setattr(FinnhubNewsConnector, "fetch", fake_fetch)

    total = scheduler.poll_finnhub_news(
        str(universe), request_spacing_seconds=1.1, sleep=sleeps.append
    )

    assert total == 6  # 3 symbols * 2 articles each
    assert calls == ["AAPL", "MSFT", "GOOG"]
    assert sleeps == [1.1, 1.1]  # spaced between calls, not before the first


def test_poll_finnhub_news_skips_a_failing_symbol(monkeypatch, tmp_path):
    universe = tmp_path / "u.txt"
    universe.write_text("aapl\nbad\nmsft\n")
    from ibkr_trader.ingestion.news.finnhub_news import FinnhubNewsConnector

    def fake_fetch(self, symbol="", **kw):
        if symbol == "BAD":
            raise RuntimeError("provider blew up")
        return 1

    monkeypatch.setattr(FinnhubNewsConnector, "fetch", fake_fetch)

    total = scheduler.poll_finnhub_news(str(universe), 0.0, sleep=lambda _s: None)
    assert total == 2  # AAPL + MSFT, BAD swallowed


def test_poll_finnhub_news_missing_universe_is_noop(tmp_path):
    total = scheduler.poll_finnhub_news(str(tmp_path / "nope.txt"), 0.0, sleep=lambda _s: None)
    assert total == 0


def test_run_prune_uses_a_session(monkeypatch):
    from contextlib import contextmanager

    calls = {}

    class FakeSession:
        pass

    @contextmanager
    def fake_get_session():
        yield FakeSession()

    def fake_prune(session, *, min_age_days):
        calls["min_age_days"] = min_age_days
        return {"news_articles": 3, "social_posts": 4}

    monkeypatch.setattr(scheduler, "get_session", fake_get_session)
    monkeypatch.setattr(scheduler, "prune_scored_raw", fake_prune)

    result = scheduler.run_prune(min_age_days=2)
    assert result == {"news_articles": 3, "social_posts": 4}
    assert calls["min_age_days"] == 2


def test_guard_swallows_and_logs_exceptions():
    def boom():
        raise RuntimeError("nope")

    guarded = scheduler._guard(boom, "boom_job")
    guarded()  # must not raise


def test_guard_runs_the_job():
    ran = {}
    scheduler._guard(lambda: ran.setdefault("x", 1), "ok")()
    assert ran == {"x": 1}
