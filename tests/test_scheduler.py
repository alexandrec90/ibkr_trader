from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from ibkr_trader import job_health, scheduler


def _settings(**overrides):
    base = dict(
        poll_newsapi_hours=12,
        newsapi_mapping_file="",
        newsapi_refresh_after_hours=12.0,
        newsapi_max_requests=90,
        scheduler_health_file="",
        db_wait_seconds=120.0,
        poll_reddit_minutes=30,
        poll_finnhub_news_hours=6,
        poll_trends_hours=24,
        prune_raw_hours=24,
        prune_raw_min_age_days=0,
        news_universe_file="tickers.txt",
        finnhub_request_spacing_seconds=1.1,
        finnhub_backfill_hours=24,
        finnhub_backfill_days=365,
        finnhub_backfill_chunk_days=30,
        finnhub_backfill_max_requests=2000,
        score_sentiment_minutes=60,
        poll_prices_hours=24,
        fx_pairs=["USDCAD"],
        trends_keywords=[],
        trends_mapping_file="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_scheduler_registers_all_jobs_with_configured_intervals():
    sched = scheduler.build_scheduler(settings=_settings())
    jobs = {job.id: job for job in sched.get_jobs()}
    assert set(jobs) == {
        "reddit_poll",
        "finnhub_news_poll",
        "newsapi_poll",
        "trends_poll",
        "prune_raw",
        "finnhub_backfill",
        "sentiment_score",
        "prices_poll",
    }
    assert jobs["reddit_poll"].trigger.interval == timedelta(minutes=30)
    assert jobs["finnhub_news_poll"].trigger.interval == timedelta(hours=6)
    assert jobs["newsapi_poll"].trigger.interval == timedelta(hours=12)
    assert jobs["trends_poll"].trigger.interval == timedelta(hours=24)
    assert jobs["prune_raw"].trigger.interval == timedelta(hours=24)
    assert jobs["finnhub_backfill"].trigger.interval == timedelta(hours=24)
    assert jobs["sentiment_score"].trigger.interval == timedelta(minutes=60)
    assert jobs["prices_poll"].trigger.interval == timedelta(hours=24)
    assert jobs["prices_poll"].next_run_time is not None  # bars catch up on startup


def test_backfill_job_also_fires_on_startup():
    """Interval jobs normally first fire one interval after start; waiting a day would let
    free-tier history roll off, so the backfill job carries an immediate next_run_time."""
    sched = scheduler.build_scheduler(settings=_settings())
    jobs = {job.id: job for job in sched.get_jobs()}
    assert jobs["finnhub_backfill"].next_run_time is not None


def test_newsapi_is_actually_scheduled():
    """It was implemented, tested and reachable only by hand — so it never ran. Regression."""
    sched = scheduler.build_scheduler(settings=_settings())
    assert "newsapi_poll" in {job.id for job in sched.get_jobs()}


def test_build_scheduler_declares_each_job_cadence_for_staleness_checks():
    job_health.reset()
    scheduler.build_scheduler(settings=_settings())

    recorded = job_health.snapshot()["jobs"]
    assert recorded["prices"]["interval_seconds"] == 24 * 3600
    assert recorded["reddit"]["interval_seconds"] == 30 * 60
    assert recorded["newsapi"]["interval_seconds"] == 12 * 3600


def test_build_scheduler_restores_history_from_a_previous_run(tmp_path):
    """A `serve` restart must not blank the record of what each job last did."""
    artifact = tmp_path / "health.json"
    job_health.reset()
    job_health.record_success("prices", 6207)
    job_health.write_artifact(artifact)
    job_health.reset()

    scheduler.build_scheduler(settings=_settings(scheduler_health_file=str(artifact)))

    entry = job_health.snapshot()["jobs"]["prices"]
    assert entry["last_success"] is not None
    assert entry["last_wrote"] is not None
    assert entry["interval_seconds"] == 24 * 3600  # and the current cadence still wins


def test_build_scheduler_honours_overridden_cadence():
    sched = scheduler.build_scheduler(settings=_settings(poll_reddit_minutes=5))
    job = {j.id: j for j in sched.get_jobs()}["reddit_poll"]
    assert job.trigger.interval == timedelta(minutes=5)


def test_poll_reddit_returns_connector_count(monkeypatch):
    from data_lake.ingestion.social.reddit import RedditConnector

    monkeypatch.setattr(RedditConnector, "fetch", lambda self, **kw: 12)
    assert scheduler.poll_reddit() == 12


def test_poll_trends_noops_without_keywords():
    assert scheduler.poll_trends([]) == 0


def test_poll_trends_calls_connector_with_keywords(monkeypatch):
    from data_lake.ingestion.social.google_trends import GoogleTrendsConnector

    seen = {}

    def fake_fetch(self, keywords=None, **kw):
        seen["keywords"] = keywords
        return len(keywords)

    monkeypatch.setattr(GoogleTrendsConnector, "fetch", fake_fetch)
    assert scheduler.poll_trends(["AAPL", "Tesla"]) == 2
    assert seen["keywords"] == ["AAPL", "Tesla"]


def test_poll_trends_pairs_sends_one_request_per_keyword(monkeypatch):
    from data_lake.ingestion.social.google_trends import GoogleTrendsConnector

    calls: list[dict] = []

    def fake_fetch(self, keywords=None, geo="", timeframe="", skip_if_newer_than_days=0.0, **kw):
        calls.append(
            {
                "keywords": keywords,
                "geo": geo,
                "timeframe": timeframe,
                "skip_days": skip_if_newer_than_days,
            }
        )
        return 5

    monkeypatch.setattr(GoogleTrendsConnector, "fetch", fake_fetch)

    total = scheduler.poll_trends_pairs([("AAPL", "Apple"), ("NVDA", "Nvidia")], geo="CA")

    assert total == 10
    # one keyword per request so each series keeps its own 0-100 scale
    assert [c["keywords"] for c in calls] == [["Apple"], ["Nvidia"]]
    assert all(c["geo"] == "CA" for c in calls)
    assert all(c["timeframe"] == "today 5-y" for c in calls)  # backfill default
    assert all(c["skip_days"] == 14.0 for c in calls)  # freshness skip on by default


def test_poll_trends_pairs_skips_a_failing_keyword(monkeypatch):
    from data_lake.ingestion.social.google_trends import GoogleTrendsConnector

    def fake_fetch(self, keywords=None, **kw):
        if keywords == ["Nvidia"]:
            raise RuntimeError("429")
        return 3

    monkeypatch.setattr(GoogleTrendsConnector, "fetch", fake_fetch)
    pairs = [("AAPL", "Apple"), ("NVDA", "Nvidia"), ("TSLA", "Tesla")]
    assert scheduler.poll_trends_pairs(pairs) == 6  # Apple + Tesla, Nvidia swallowed


def test_poll_trends_mapping_missing_file_is_noop(tmp_path):
    assert scheduler.poll_trends_mapping(str(tmp_path / "nope.txt")) == 0


def test_poll_trends_job_prefers_mapping_file(monkeypatch, tmp_path):
    mapping = tmp_path / "m.txt"
    mapping.write_text("AAPL,Apple\n", encoding="utf-8")
    seen = {}
    monkeypatch.setattr(
        scheduler, "poll_trends_mapping", lambda path, **kw: seen.setdefault("path", path) and 7
    )
    settings = _settings(trends_mapping_file=str(mapping), trends_keywords=["ignored"])
    scheduler.poll_trends_job(settings)
    assert seen["path"] == str(mapping)


def test_poll_trends_job_falls_back_to_keywords(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        scheduler, "poll_trends", lambda keywords: seen.setdefault("keywords", keywords) and 0
    )
    settings = _settings(
        trends_mapping_file=str(tmp_path / "absent.txt"), trends_keywords=["Tesla"]
    )
    scheduler.poll_trends_job(settings)
    assert seen["keywords"] == ["Tesla"]


def _patch_newsapi(monkeypatch, fake_fetch, fresh: set[str] | None = None):
    from data_lake.ingestion.news import newsapi

    monkeypatch.setattr(newsapi.NewsApiConnector, "fetch", fake_fetch)
    monkeypatch.setattr(newsapi, "fresh_tagged_symbols", lambda cutoff: fresh or set())


def test_poll_newsapi_pairs_skips_fresh_symbols(monkeypatch):
    calls: list[dict] = []

    def fake_fetch(self, query="", symbol="", **kw):
        calls.append({"query": query, "symbol": symbol})
        return 4

    _patch_newsapi(monkeypatch, fake_fetch, fresh={"AAPL"})

    total = scheduler.poll_newsapi_pairs([("AAPL", "Apple Inc"), ("NVDA", "Nvidia")])

    assert total == 4
    assert calls == [{"query": "Nvidia", "symbol": "NVDA"}]  # AAPL fresh — no request spent


def test_poll_newsapi_pairs_respects_request_budget(monkeypatch):
    calls: list[str] = []

    def fake_fetch(self, query="", symbol="", **kw):
        calls.append(symbol)
        return 1

    _patch_newsapi(monkeypatch, fake_fetch)

    total = scheduler.poll_newsapi_pairs([("A", "a"), ("B", "b"), ("C", "c")], max_requests=2)

    assert calls == ["A", "B"]  # budget stops the run; C stays stale for the next run
    assert total == 2


def test_poll_newsapi_pairs_aborts_on_fatal_status(monkeypatch):
    from data_lake.ingestion.news.newsapi import NewsApiProviderError

    calls: list[str] = []

    def fake_fetch(self, query="", symbol="", **kw):
        calls.append(symbol)
        if symbol == "B":
            raise NewsApiProviderError("quota gone", status_code=429)
        return 1

    _patch_newsapi(monkeypatch, fake_fetch)

    total = scheduler.poll_newsapi_pairs([("A", "a"), ("B", "b"), ("C", "c")])

    assert calls == ["A", "B"]  # C not attempted — every further call would 429 too
    assert total == 1


def test_poll_newsapi_pairs_skips_a_failing_query(monkeypatch):
    from data_lake.ingestion.news.newsapi import NewsApiProviderError

    def fake_fetch(self, query="", symbol="", **kw):
        if symbol == "B":
            raise NewsApiProviderError("400 bad query", status_code=400)
        return 1

    _patch_newsapi(monkeypatch, fake_fetch)
    # non-fatal per-query failure is swallowed; the rest of the list still runs
    assert scheduler.poll_newsapi_pairs([("A", "a"), ("B", "b"), ("C", "c")]) == 2


def test_poll_newsapi_pairs_zero_refresh_disables_freshness_check(monkeypatch):
    from data_lake.ingestion.news import newsapi

    def boom(cutoff):
        raise AssertionError("freshness check must not run when refresh_after_hours=0")

    monkeypatch.setattr(newsapi.NewsApiConnector, "fetch", lambda self, **kw: 1)
    monkeypatch.setattr(newsapi, "fresh_tagged_symbols", boom)

    assert scheduler.poll_newsapi_pairs([("A", "a")], refresh_after_hours=0) == 1


def test_poll_finnhub_news_iterates_universe_and_spaces_calls(monkeypatch, tmp_path):
    universe = tmp_path / "u.txt"
    universe.write_text("aapl\nmsft\ngoog\n")
    from data_lake.ingestion.news.finnhub_news import FinnhubNewsConnector

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


def test_poll_finnhub_news_passes_backfill_window_through(monkeypatch, tmp_path):
    universe = tmp_path / "u.txt"
    universe.write_text("aapl\n")
    from data_lake.ingestion.news.finnhub_news import FinnhubNewsConnector

    seen = {}

    def fake_fetch(self, symbol="", date_from="", date_to="", **kw):
        seen.update(date_from=date_from, date_to=date_to)
        return 1

    monkeypatch.setattr(FinnhubNewsConnector, "fetch", fake_fetch)

    scheduler.poll_finnhub_news(
        str(universe), 0.0, sleep=lambda _s: None, date_from="2025-07-16", date_to="2026-07-15"
    )
    assert seen == {"date_from": "2025-07-16", "date_to": "2026-07-15"}


def test_poll_finnhub_news_skips_a_failing_symbol(monkeypatch, tmp_path):
    universe = tmp_path / "u.txt"
    universe.write_text("aapl\nbad\nmsft\n")
    from data_lake.ingestion.news.finnhub_news import FinnhubNewsConnector

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


def test_backfill_finnhub_news_reads_universe_and_delegates(monkeypatch, tmp_path):
    universe = tmp_path / "tickers.txt"
    universe.write_text("aapl\nMSFT\n")
    seen = {}

    def fake_run_backfill(symbols, **kwargs):
        seen["symbols"] = symbols
        seen["kwargs"] = kwargs
        return 7

    from data_lake.ingestion.news import finnhub_backfill

    monkeypatch.setattr(finnhub_backfill, "run_backfill", fake_run_backfill)

    count = scheduler.backfill_finnhub_news(
        str(universe),
        backfill_days=365,
        chunk_days=30,
        max_requests=2000,
        request_spacing_seconds=1.1,
    )
    assert count == 7
    assert seen["symbols"] == ["AAPL", "MSFT"]
    assert seen["kwargs"]["backfill_days"] == 365
    assert seen["kwargs"]["max_requests"] == 2000


def test_backfill_finnhub_news_missing_universe_is_noop(tmp_path):
    assert (
        scheduler.backfill_finnhub_news(
            str(tmp_path / "absent.txt"),
            backfill_days=365,
            chunk_days=30,
            max_requests=2000,
            request_spacing_seconds=1.1,
        )
        == 0
    )


def _sqlite_session_scope():
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from ibkr_trader.db.models import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session = factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    return scope


def test_poll_yahoo_prices_refreshes_each_tracked_symbol(monkeypatch):
    from datetime import UTC, datetime

    from data_lake.ingestion.market.yahoo import YahooConnector

    from ibkr_trader.db.models import Instrument, PriceBar

    scope = _sqlite_session_scope()
    with scope() as session:
        for symbol, exchange, currency in (("XEQT", "TSX", "CAD"), ("AAPL", "SMART", "USD")):
            instrument = Instrument(symbol=symbol, exchange=exchange, currency=currency)
            session.add(instrument)
            session.flush()
            session.add(
                PriceBar(
                    instrument_id=instrument.id,
                    ts=datetime(2026, 7, 7, tzinfo=UTC),
                    bar_size="1 day",
                    source="yahoo",
                    what_to_show="ADJUSTED_LAST",
                    open=1.0,
                    high=1.0,
                    low=1.0,
                    close=1.0,
                    volume=1.0,
                )
            )

    fetched: list[str] = []

    def fake_fetch(self, symbol="", **kwargs):
        fetched.append(symbol)
        if symbol == "AAPL":
            raise RuntimeError("yahoo hiccup")  # must be skipped, not fatal
        return 3

    monkeypatch.setattr(scheduler, "get_session", scope)
    monkeypatch.setattr(YahooConnector, "fetch", fake_fetch)

    assert scheduler.poll_yahoo_prices() == 3
    assert fetched == ["AAPL", "XEQT.TO"]


def test_poll_fx_starts_from_newest_stored_bar(monkeypatch):
    from datetime import UTC, datetime

    from data_lake.ingestion.market.fmp_fx import FmpFxConnector
    from data_lake.ingestion.market.yahoo_fx import YahooFxConnector

    from ibkr_trader.db.models import Instrument, PriceBar

    scope = _sqlite_session_scope()
    with scope() as session:
        instrument = Instrument(symbol="USDCAD", exchange="IDEALPRO", currency="CAD")
        session.add(instrument)
        session.flush()
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                ts=datetime(2026, 7, 7, tzinfo=UTC),
                bar_size="1 day",
                source="fmp",
                what_to_show="TRADES",
                open=1.3,
                high=1.3,
                low=1.3,
                close=1.3,
                volume=None,
            )
        )

    seen = {}
    yahoo_pairs = []

    def fake_fmp_fetch(self, pair="", date_from="", **kwargs):
        seen[pair] = date_from
        return 5

    def fake_yahoo_fetch(self, pair="", **kwargs):
        yahoo_pairs.append(pair)
        return 2

    monkeypatch.setattr(scheduler, "get_session", scope)
    monkeypatch.setattr(FmpFxConnector, "fetch", fake_fmp_fetch)
    monkeypatch.setattr(YahooFxConnector, "fetch", fake_yahoo_fetch)

    assert scheduler.poll_fx(["USDCAD"]) == 7  # both providers refreshed per pair
    assert seen == {"USDCAD": "2026-07-04"}  # newest bar minus the 3-day overlap
    assert yahoo_pairs == ["USDCAD"]  # yahoo does its own incremental bookkeeping


def test_poll_fx_empty_history_fetches_full_range(monkeypatch):
    from data_lake.ingestion.market.fmp_fx import FmpFxConnector
    from data_lake.ingestion.market.yahoo_fx import YahooFxConnector

    scope = _sqlite_session_scope()
    seen = {}

    def fake_fetch(self, pair="", date_from="", **kwargs):
        seen[pair] = date_from
        return 9

    monkeypatch.setattr(scheduler, "get_session", scope)
    monkeypatch.setattr(FmpFxConnector, "fetch", fake_fetch)
    monkeypatch.setattr(YahooFxConnector, "fetch", lambda self, pair="", **kwargs: 0)

    assert scheduler.poll_fx(["EURCAD"]) == 9
    assert seen == {"EURCAD": ""}  # no stored bars -> connector default (full history)


def test_poll_fx_provider_failures_are_isolated(monkeypatch):
    """One provider blowing up must not stop the other (or the pair loop)."""
    from data_lake.ingestion.market.fmp_fx import FmpFxConnector
    from data_lake.ingestion.market.yahoo_fx import YahooFxConnector

    scope = _sqlite_session_scope()

    def broken_fmp(self, pair="", date_from="", **kwargs):
        raise RuntimeError("fmp down")

    monkeypatch.setattr(scheduler, "get_session", scope)
    monkeypatch.setattr(FmpFxConnector, "fetch", broken_fmp)
    monkeypatch.setattr(YahooFxConnector, "fetch", lambda self, pair="", **kwargs: 4)

    assert scheduler.poll_fx(["USDCAD"]) == 4  # yahoo still contributed


def test_run_sentiment_scoring_uses_a_session(monkeypatch):
    from contextlib import contextmanager

    from ibkr_trader.signals import sentiment

    class FakeSession:
        pass

    @contextmanager
    def fake_get_session():
        yield FakeSession()

    monkeypatch.setattr(scheduler, "get_session", fake_get_session)
    monkeypatch.setattr(
        sentiment, "score_pending", lambda session: {"news_articles": 2, "social_posts": 1}
    )

    assert scheduler.run_sentiment_scoring() == {"news_articles": 2, "social_posts": 1}


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

    guarded = scheduler._guard(boom, "boom_job", artifact_path="")
    guarded()  # must not raise


def test_guard_runs_the_job():
    ran = {}
    scheduler._guard(lambda: ran.setdefault("x", 1), "ok", artifact_path="")()
    assert ran == {"x": 1}


# --- health recording -------------------------------------------------------------------
# The regression these cover: a guarded job that raises used to leave nothing behind except a
# log line that APScheduler immediately followed with "executed successfully".


def test_guard_records_a_success_with_its_result():
    job_health.reset()
    scheduler._guard(lambda: 809, "prices", artifact_path="")()

    entry = job_health.snapshot()["jobs"]["prices"]
    assert entry["last_success"] is not None
    assert entry["last_result"] == "809"


def test_guard_records_a_failure_with_its_error():
    job_health.reset()

    def boom():
        raise RuntimeError("db is gone")

    scheduler._guard(boom, "prices", artifact_path="")()

    entry = job_health.snapshot()["jobs"]["prices"]
    assert entry["consecutive_failures"] == 1
    assert entry["last_error"] == "RuntimeError: db is gone"
    assert entry["last_success"] is None


def test_guard_writes_the_health_artifact_after_a_failed_run(tmp_path):
    """The artifact has to be written on failure too, not only on success."""
    job_health.reset()
    artifact = tmp_path / "health.json"

    def boom():
        raise RuntimeError("db is gone")

    scheduler._guard(boom, "prices", artifact_path=str(artifact))()

    assert job_health.load_artifact(artifact)["jobs"]["prices"]["last_error"]


# --- transient-failure retry ------------------------------------------------------------


class _FakeScheduler:
    """Records add_job calls; enough surface for the retry path."""

    def __init__(self):
        self.jobs = []

    def add_job(self, func, trigger=None, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})


def _operational_error():
    from sqlalchemy.exc import OperationalError

    return OperationalError("SELECT 1", {}, Exception("could not translate host name 'db'"))


def test_guard_retries_a_database_failure_instead_of_waiting_a_full_interval():
    """The 24 h-gap bug: a startup job that met a booting database waited a whole day."""
    job_health.reset()
    fake = _FakeScheduler()

    def boom():
        raise _operational_error()

    scheduler._guard(boom, "prices", scheduler=fake, artifact_path="")()

    assert len(fake.jobs) == 1
    retry = fake.jobs[0]
    assert retry["trigger"] == "date"
    assert retry["id"] == "prices_retry"
    assert retry["replace_existing"] is True


def test_retry_delay_backs_off_with_each_consecutive_failure():
    job_health.reset()
    fake = _FakeScheduler()

    def boom():
        raise _operational_error()

    guarded = scheduler._guard(
        boom, "prices", scheduler=fake, retry_delays=(300, 900), artifact_path=""
    )
    guarded()
    guarded()

    gaps = [(job["run_date"] - datetime.now(UTC)).total_seconds() for job in fake.jobs]
    assert 280 < gaps[0] < 310  # ~5 min
    assert 880 < gaps[1] < 910  # ~15 min


def test_retries_stop_after_the_delay_list_is_exhausted():
    """Otherwise a permanently dead database would be retried forever."""
    job_health.reset()
    fake = _FakeScheduler()

    def boom():
        raise _operational_error()

    guarded = scheduler._guard(
        boom, "prices", scheduler=fake, retry_delays=(300,), artifact_path=""
    )
    guarded()
    guarded()
    guarded()

    assert len(fake.jobs) == 1  # only the first failure earned a retry


def test_a_non_transient_failure_is_not_retried():
    """A missing credential will not fix itself in five minutes; retrying is just noise."""
    job_health.reset()
    fake = _FakeScheduler()

    def boom():
        raise RuntimeError("REDDIT_CLIENT_ID/SECRET not set")

    scheduler._guard(boom, "reddit", scheduler=fake, artifact_path="")()

    assert fake.jobs == []


def test_a_successful_run_clears_the_streak_so_retries_start_over():
    job_health.reset()
    fake = _FakeScheduler()
    outcomes = [_operational_error(), None, _operational_error()]

    def flaky():
        result = outcomes.pop(0)
        if result is not None:
            raise result

    guarded = scheduler._guard(flaky, "prices", scheduler=fake, artifact_path="")
    guarded()
    guarded()
    guarded()

    gaps = [(job["run_date"] - datetime.now(UTC)).total_seconds() for job in fake.jobs]
    assert len(gaps) == 2
    assert 280 < gaps[1] < 310  # back to the first delay, not escalated


def test_guard_without_a_scheduler_still_records_the_failure():
    job_health.reset()

    def boom():
        raise _operational_error()

    scheduler._guard(boom, "prices", scheduler=None, artifact_path="")()  # must not raise
    assert job_health.snapshot()["jobs"]["prices"]["consecutive_failures"] == 1


# --- startup database wait --------------------------------------------------------------


def test_wait_for_database_returns_immediately_when_reachable(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def ok_session():
        yield SimpleNamespace(execute=lambda _stmt: None)

    monkeypatch.setattr(scheduler, "get_session", ok_session)
    slept: list[float] = []

    assert scheduler.wait_for_database(60.0, sleep=slept.append) is True
    assert slept == []


def test_wait_for_database_retries_until_the_database_answers(monkeypatch):
    from contextlib import contextmanager

    attempts = {"n": 0}

    @contextmanager
    def flaky_session():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _operational_error()
        yield SimpleNamespace(execute=lambda _stmt: None)

    monkeypatch.setattr(scheduler, "get_session", flaky_session)
    slept: list[float] = []

    assert scheduler.wait_for_database(60.0, sleep=slept.append) is True
    assert attempts["n"] == 3
    assert slept == [1.0, 2.0]  # exponential backoff between probes


def test_wait_for_database_gives_up_at_the_timeout(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def dead_session():
        raise _operational_error()
        yield  # pragma: no cover - unreachable, keeps this a generator

    clock = {"t": 0.0}

    def fake_now():
        return clock["t"]

    def fake_sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(scheduler, "get_session", dead_session)

    assert scheduler.wait_for_database(10.0, sleep=fake_sleep, now=fake_now) is False


def test_wait_for_database_never_raises(monkeypatch):
    """A surprising error here must not stop `serve` from starting — jobs retry anyway."""
    from contextlib import contextmanager

    @contextmanager
    def exploding_session():
        raise ValueError("something unexpected")
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(scheduler, "get_session", exploding_session)

    assert scheduler.wait_for_database(0.0, sleep=lambda _s: None) is False


# --- the newsapi job --------------------------------------------------------------------


def test_poll_newsapi_job_batches_the_mapping_file(monkeypatch, tmp_path):
    mapping = tmp_path / "news-keywords.txt"
    mapping.write_text("AAPL,Apple Inc\nNVDA,Nvidia\n", encoding="utf-8")
    seen = {}

    def fake_pairs(pairs, refresh_after_hours=0.0, max_requests=0):
        seen.update(pairs=pairs, refresh=refresh_after_hours, budget=max_requests)
        return 11

    monkeypatch.setattr(scheduler, "poll_newsapi_pairs", fake_pairs)
    settings = _settings(
        newsapi_mapping_file=str(mapping), newsapi_refresh_after_hours=8.0, newsapi_max_requests=50
    )

    assert scheduler.poll_newsapi_job(settings) == 11
    assert seen["pairs"] == [("AAPL", "Apple Inc"), ("NVDA", "Nvidia")]
    assert seen["refresh"] == 8.0
    assert seen["budget"] == 50


def test_poll_newsapi_job_missing_mapping_file_is_noop(tmp_path):
    settings = _settings(newsapi_mapping_file=str(tmp_path / "absent.txt"))
    assert scheduler.poll_newsapi_job(settings) == 0


def test_poll_newsapi_job_unset_mapping_file_is_noop():
    assert scheduler.poll_newsapi_job(_settings(newsapi_mapping_file="")) == 0
