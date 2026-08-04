"""APScheduler wiring for `serve`: periodic ingestion polls + raw-pruning maintenance.

No trading here — the paper loop stays out until backtests justify it (see TODO §4). Each job
is wrapped by ``_guard`` so a single failure logs and the scheduler keeps running rather than
dying. Cadence and rate-limit spacing come from ``Settings`` (all off unless `serve` runs).

Two things ``_guard`` does beyond swallowing the exception, both learned from a six-day silent
outage (the database was stopped; every run logged "executed successfully" anyway):

- **it records the outcome** to ``job_health``, which persists a parseable artifact that
  `ibkr-trader health` reads. Swallowing a failure is right; hiding it is not.
- **it retries a job that failed on a dead database**, instead of leaving a daily job to wait
  a full day. Only connection-shaped errors are retried — a missing credential will not fix
  itself in five minutes, so retrying it would just be noise.
"""

import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from ibkr_trader import job_health
from ibkr_trader.config import Settings, get_settings
from ibkr_trader.db.models import Instrument, PriceBar
from ibkr_trader.db.session import get_session
from ibkr_trader.maintenance import prune_scored_raw

logger = logging.getLogger(__name__)

#: Failures worth retrying sooner than the next scheduled run: the database was unreachable or
#: the connection dropped. Anything else (bad credentials, a provider rejecting the request) is
#: recorded and left to its normal cadence.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (OperationalError, InterfaceError, DBAPIError)

#: Backoff for those retries. Deliberately short-then-long: a container that started before its
#: database usually recovers within seconds, and a longer outage should not spin.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (300, 900, 3600)


def _read_universe_file(path: str) -> list[str]:
    try:
        with open(path) as handle:
            return [line.strip().upper() for line in handle if line.strip()]
    except OSError:
        logger.warning("universe file %r not readable; finnhub-news poll is a no-op", path)
        return []


def poll_reddit() -> int:
    from data_lake.ingestion.social.reddit import RedditConnector

    count = RedditConnector().fetch()
    logger.info("reddit poll upserted %d posts", count)
    return count


def poll_trends(keywords: list[str]) -> int:
    if not keywords:
        logger.info("trends poll skipped: no trends_keywords configured")
        return 0
    from data_lake.ingestion.social.google_trends import GoogleTrendsConnector

    count = GoogleTrendsConnector().fetch(keywords=keywords)
    logger.info("trends poll upserted %d points", count)
    return count


def poll_trends_pairs(
    pairs: list[tuple[str, str]],
    geo: str = "",
    timeframe: str = "today 5-y",
    refresh_after_days: float = 14.0,
) -> int:
    """Fetch interest for (symbol, search-term) pairs, ONE keyword per request.

    One keyword per request keeps every series on its own 0-100 scale — a multi-keyword
    payload is normalized against the batch max, which crushes low-volume terms into
    quantization noise. The connector's module throttle spaces requests ~60 s apart, so N
    keywords take ~N minutes; a failing keyword is logged and skipped. Default timeframe
    ``today 5-y`` returns ~5 years of weekly points, so the first run doubles as backfill.

    ``refresh_after_days`` skips keywords whose stored series is already fresh: weekly
    buckets mean a keyword fetched < 14 days ago can have nothing new (the newest complete
    week is always 7-13 days old), so reruns cost seconds and a partially-failed batch
    resumes from the failed keywords. Pass 0 to force a full re-fetch.
    """
    from data_lake.ingestion.social.google_trends import GoogleTrendsConnector

    connector = GoogleTrendsConnector()
    total = 0
    fetched = 0
    for symbol, keyword in pairs:
        try:
            points = connector.fetch(
                keywords=[keyword],
                geo=geo,
                timeframe=timeframe,
                skip_if_newer_than_days=refresh_after_days,
            )
        except Exception:
            logger.exception("trends poll failed for %s (%r)", symbol, keyword)
        else:
            total += points
            fetched += 1 if points else 0
    logger.info(
        "trends poll upserted %d points across %d keywords (%d fetched, rest fresh/failed)",
        total,
        len(pairs),
        fetched,
    )
    return total


def poll_trends_mapping(mapping_file: str, geo: str = "", timeframe: str = "today 5-y") -> int:
    """Batch-poll Trends from a ``TICKER,search term`` mapping file (soft no-op if missing)."""
    from data_lake.ingestion.social.google_trends import read_mapping_file

    try:
        pairs = read_mapping_file(mapping_file)
    except OSError:
        logger.warning("trends mapping file %r not readable; trends poll is a no-op", mapping_file)
        return 0
    return poll_trends_pairs(pairs, geo=geo, timeframe=timeframe)


def poll_trends_job(settings: Settings) -> int:
    """The scheduled trends job: mapping file when present, else ad-hoc trends_keywords."""
    if settings.trends_mapping_file and Path(settings.trends_mapping_file).exists():
        return poll_trends_mapping(settings.trends_mapping_file)
    return poll_trends(settings.trends_keywords)


def poll_newsapi_pairs(
    pairs: list[tuple[str, str]],
    refresh_after_hours: float = 12.0,
    max_requests: int = 90,
) -> int:
    """Fetch NewsAPI headlines for (symbol, query) pairs, one request per pair.

    Free-tier protections, in order:
    - **freshness skip**: a symbol whose newsapi articles were fetched within
      ``refresh_after_hours`` is skipped without spending a request (free-tier articles are
      delayed 24 h, so re-polling sooner than ~12 h can't surface anything new);
    - **request budget**: at most ``max_requests`` requests per run (free tier is 100/day —
      the default leaves headroom for ad-hoc queries);
    - **fatal abort**: a 401/426/429 response means every remaining call would fail too
      (bad key / plan limit / quota exhausted), so the run stops instead of failing through
      the whole list. Other per-query failures are logged and skipped.
    """
    from datetime import UTC, datetime

    from data_lake.ingestion.news.newsapi import (
        FATAL_STATUSES,
        NewsApiConnector,
        NewsApiProviderError,
        fresh_tagged_symbols,
    )

    fresh: set[str] = set()
    if refresh_after_hours > 0:
        cutoff = datetime.now(UTC) - timedelta(hours=refresh_after_hours)
        fresh = fresh_tagged_symbols(cutoff)

    connector = NewsApiConnector()
    total = 0
    requests_used = 0
    skipped_fresh = 0
    for index, (symbol, query) in enumerate(pairs):
        if symbol in fresh:
            skipped_fresh += 1
            continue
        if requests_used >= max_requests:
            logger.warning(
                "newsapi poll stopped at the %d-request budget; %d pair(s) left for a later run",
                max_requests,
                len(pairs) - index,
            )
            break
        requests_used += 1
        try:
            total += connector.fetch(query=query, symbol=symbol)
        except NewsApiProviderError as exc:
            if exc.status_code in FATAL_STATUSES:
                logger.error(
                    "newsapi poll aborted at %s (%r): %s — remaining %d pair(s) would fail too",
                    symbol,
                    query,
                    exc,
                    len(pairs) - index - 1,
                )
                break
            logger.exception("newsapi poll failed for %s (%r)", symbol, query)
        except Exception:
            logger.exception("newsapi poll failed for %s (%r)", symbol, query)
    logger.info(
        "newsapi poll upserted %d articles across %d pairs (%d requests, %d fresh-skipped)",
        total,
        len(pairs),
        requests_used,
        skipped_fresh,
    )
    return total


def poll_newsapi_job(settings: Settings) -> int:
    """The scheduled NewsAPI job: batch the mapping file, or no-op when it is absent.

    ``poll_newsapi_pairs`` existed with full free-tier protection long before anything called
    it on a schedule — it was reachable only through `ingest news --mapping-file`, so NewsAPI
    was a manual source that nobody ran manually.
    """
    mapping_file = getattr(settings, "newsapi_mapping_file", "")
    if not mapping_file or not Path(mapping_file).exists():
        logger.info("newsapi poll skipped: mapping file %r not present", mapping_file)
        return 0
    from data_lake.ingestion.social.google_trends import read_mapping_file

    try:
        pairs = read_mapping_file(mapping_file)
    except (OSError, ValueError):
        logger.warning("newsapi mapping file %r not readable; poll is a no-op", mapping_file)
        return 0
    return poll_newsapi_pairs(
        pairs,
        refresh_after_hours=settings.newsapi_refresh_after_hours,
        max_requests=settings.newsapi_max_requests,
    )


def poll_finnhub_news(
    universe_file: str,
    request_spacing_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    date_from: str = "",
    date_to: str = "",
) -> int:
    """Fetch company news for every universe symbol, spacing calls under the free-tier limit.

    A per-symbol failure is logged and skipped so one bad ticker doesn't abort the whole poll.
    Re-runs are cheap by construction: it is one API call per symbol regardless of window, and
    overlapping articles are idempotent upserts. ``date_from``/``date_to`` widen the window
    (default: connector's last-7-days) — pass ~1 year for the initial backfill.
    """
    from data_lake.ingestion.news.finnhub_news import FinnhubNewsConnector

    symbols = _read_universe_file(universe_file)
    connector = FinnhubNewsConnector()
    total = 0
    for index, symbol in enumerate(symbols):
        if index:
            sleep(request_spacing_seconds)
        try:
            total += connector.fetch(symbol=symbol, date_from=date_from, date_to=date_to)
        except Exception:
            logger.exception("finnhub-news poll failed for %s", symbol)
    logger.info("finnhub-news poll upserted %d articles across %d symbols", total, len(symbols))
    return total


def backfill_finnhub_news(
    universe_file: str,
    *,
    backfill_days: int,
    chunk_days: int,
    max_requests: int,
    request_spacing_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Walk each universe symbol's Finnhub news history back to the rolling free-tier floor.

    Thin wrapper over ``finnhub_backfill.run_backfill`` (which owns the cursor/split/budget
    logic); missing universe file is a soft no-op like the regular poll.
    """
    from data_lake.ingestion.news.finnhub_backfill import run_backfill

    symbols = _read_universe_file(universe_file)
    if not symbols:
        return 0
    count = run_backfill(
        symbols,
        backfill_days=backfill_days,
        chunk_days=chunk_days,
        max_requests=max_requests,
        request_spacing_seconds=request_spacing_seconds,
        sleep=sleep,
    )
    logger.info("finnhub backfill upserted %d articles across %d symbols", count, len(symbols))
    return count


def poll_yahoo_prices() -> int:
    """Incremental Yahoo daily-bar refresh for every instrument Yahoo already tracks.

    The connector fetches only bars newer than the newest stored one (returning 0 without a
    request when already current) and throttles itself, so a machine that was off for days
    simply catches up on the next run and a same-day rerun is nearly free. A failing symbol
    is logged and skipped.
    """
    from data_lake.ingestion.market.yahoo import YahooConnector
    from data_lake.ingestion.market.yahoo_common import tracked_yahoo_symbols

    with get_session() as session:
        symbols = tracked_yahoo_symbols(session)
    connector = YahooConnector()
    total = 0
    failures = 0
    for symbol in symbols:
        try:
            total += connector.fetch(symbol=symbol)
        except Exception:
            failures += 1
            logger.exception("price poll failed for %s", symbol)
    logger.info(
        "price poll upserted %d bars across %d symbols (%d failed)",
        total,
        len(symbols),
        failures,
    )
    return total


#: Overlap fetched before the newest stored FX bar — re-upserts are idempotent, and the small
#: overlap heals late corrections around the boundary day.
_FX_REFRESH_OVERLAP_DAYS = 3


def poll_fx(pairs: list[str]) -> int:
    """Refresh daily FX bars from the newest stored bar forward (full history when empty).

    Both providers are refreshed per pair: FMP (the original series) and Yahoo (the deep
    ~2003+ backfill, see yahoo_fx.py). The backtest loader picks exactly one source per
    window by widest coverage, so keeping both current stops a long window from ever
    preferring a stale-but-longer series. Each provider fails independently.
    """

    from data_lake.ingestion.market.fmp_fx import FmpFxConnector
    from data_lake.ingestion.market.yahoo_fx import YahooFxConnector

    fmp_connector = FmpFxConnector()
    yahoo_connector = YahooFxConnector()
    total = 0
    for pair in pairs:
        with get_session() as session:
            newest = session.scalar(
                select(func.max(PriceBar.ts))
                .join(Instrument, Instrument.id == PriceBar.instrument_id)
                .where(Instrument.symbol == pair.strip().upper())
            )
        date_from = (
            (newest.date() - timedelta(days=_FX_REFRESH_OVERLAP_DAYS)).isoformat() if newest else ""
        )
        try:
            total += fmp_connector.fetch(pair=pair, date_from=date_from)
        except Exception:
            logger.exception("fmp fx poll failed for %s", pair)
        try:
            # incremental from the newest yahoo-source bar; when the yahoo series doesn't
            # exist yet this is the full-history deep backfill (one request, self-healing)
            total += yahoo_connector.fetch(pair=pair)
        except Exception:
            logger.exception("yahoo fx poll failed for %s", pair)
    logger.info("fx poll upserted %d bars across %d pairs", total, len(pairs))
    return total


def poll_prices_job(settings: Settings) -> int:
    """The scheduled market-data job: Yahoo equity/ETF bars, then FX pairs."""
    return poll_yahoo_prices() + poll_fx(settings.fx_pairs)


def run_sentiment_scoring() -> dict[str, int]:
    from ibkr_trader.signals.sentiment import score_pending

    with get_session() as session:
        counts = score_pending(session)
    logger.info("sentiment scoring filled: %s", counts)
    return counts


def run_prune(min_age_days: int) -> dict[str, int]:
    with get_session() as session:
        counts = prune_scored_raw(session, min_age_days=min_age_days)
    logger.info("prune dropped raw payloads: %s", counts)
    return counts


def _schedule_retry(
    scheduler: BlockingScheduler | None,
    wrapped: Callable[[], None],
    label: str,
    attempt: int,
    retry_delays: Sequence[int],
) -> bool:
    """Queue a one-shot retry after a transient failure. Returns whether one was scheduled.

    Without this, a job that fires at startup (prices, the Finnhub backfill) and finds the
    database still coming up burns its only run and waits a full 24 h — which is exactly how a
    restart during a database outage cost a day of bars.
    """
    if scheduler is None or not retry_delays:
        return False
    if attempt > len(retry_delays):
        logger.error(
            "job %r has failed %d times in a row; leaving it to its next scheduled run",
            label,
            attempt,
        )
        return False
    delay = retry_delays[attempt - 1]
    try:
        scheduler.add_job(
            wrapped,
            "date",
            run_date=datetime.now(UTC) + timedelta(seconds=delay),
            id=f"{label}_retry",
            replace_existing=True,
        )
    except Exception:  # a scheduler that is shutting down, say — never fail the job for this
        logger.exception("could not schedule a retry for %r", label)
        return False
    logger.warning("job %r failed transiently; retrying in %ds (attempt %d)", label, delay, attempt)
    return True


def _guard(
    job: Callable[[], object],
    label: str,
    *,
    scheduler: BlockingScheduler | None = None,
    retry_delays: Sequence[int] = RETRY_DELAYS_SECONDS,
    artifact_path: str = job_health.DEFAULT_ARTIFACT,
) -> Callable[[], None]:
    """Wrap a job so an exception is logged and recorded, not propagated.

    Keeping the scheduler alive is the point, but a swallowed failure that leaves no trace is
    how ingestion died quietly for six days. Every outcome lands in ``job_health`` and is
    flushed to the health artifact; transient (database-connection) failures also earn a retry.
    """

    def wrapped() -> None:
        try:
            result = job()
        except Exception as exc:
            logger.exception("scheduled job %r failed", label)
            attempt = job_health.record_failure(label, exc)
            if isinstance(exc, TRANSIENT_ERRORS):
                _schedule_retry(scheduler, wrapped, label, attempt, retry_delays)
        else:
            job_health.record_success(label, result)
        job_health.write_artifact(artifact_path)

    return wrapped


def wait_for_database(
    timeout_seconds: float = 120.0,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> bool:
    """Block until the database answers ``SELECT 1``, or the timeout expires. Never raises.

    Compose's ``depends_on: service_healthy`` only orders a ``compose up``; when the daemon
    restarts containers after a reboot the app can come back before its database. The jobs that
    fire at startup would then fail instantly, so this gives them something to start against.
    A ``False`` return is not fatal — jobs retry — but it is worth one loud log line.
    """
    deadline = now() + timeout_seconds
    delay = 1.0
    attempts = 0
    while True:
        attempts += 1
        try:
            with get_session() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:
            remaining = deadline - now()
            if remaining <= 0:
                logger.error(
                    "database still unreachable after %.0fs (%d attempts): %s",
                    timeout_seconds,
                    attempts,
                    exc,
                )
                return False
            logger.warning(
                "database not ready (attempt %d): %s; retrying in %.0fs", attempts, exc, delay
            )
            sleep(min(delay, remaining))
            delay = min(delay * 2, 15.0)
        else:
            if attempts > 1:
                logger.info("database reachable after %d attempt(s)", attempts)
            return True


def build_scheduler(
    settings: Settings | None = None,
    scheduler: BlockingScheduler | None = None,
) -> BlockingScheduler:
    """Register the periodic jobs on a scheduler and return it (unstarted, so it's testable)."""
    from ibkr_trader.lake import configure_lake

    # The jobs below drive connectors from the shared data_lake package, which owns no config
    # and no engine. Wire it here too, not only in the CLI callback, so a caller that builds
    # the scheduler directly gets a working one.
    configure_lake()
    settings = settings or get_settings()
    scheduler = scheduler or BlockingScheduler(timezone="UTC")
    artifact_path = getattr(settings, "scheduler_health_file", job_health.DEFAULT_ARTIFACT)
    # Carry the previous process's history forward: a restart is not evidence that anything
    # ingested, and a registry that starts empty reports every job as never-run for a full
    # cadence afterwards.
    job_health.seed_from_artifact(artifact_path)

    def register(
        job_id: str,
        label: str,
        job: Callable[[], object],
        *,
        seconds: float,
        start_now: bool = False,
    ) -> None:
        """Add one guarded interval job, and tell ``job_health`` what cadence to expect."""
        job_health.record_schedule(label, seconds)
        extra = {"next_run_time": datetime.now(UTC)} if start_now else {}
        scheduler.add_job(
            _guard(job, label, scheduler=scheduler, artifact_path=artifact_path),
            "interval",
            seconds=seconds,
            id=job_id,
            **extra,
        )

    register("reddit_poll", "reddit", poll_reddit, seconds=settings.poll_reddit_minutes * 60)
    register(
        "finnhub_news_poll",
        "finnhub_news",
        lambda: poll_finnhub_news(
            settings.news_universe_file, settings.finnhub_request_spacing_seconds
        ),
        seconds=settings.poll_finnhub_news_hours * 3600,
    )
    register(
        "newsapi_poll",
        "newsapi",
        lambda: poll_newsapi_job(settings),
        seconds=settings.poll_newsapi_hours * 3600,
    )
    register(
        "trends_poll",
        "trends",
        lambda: poll_trends_job(settings),
        seconds=settings.poll_trends_hours * 3600,
    )
    register(
        "prune_raw",
        "prune",
        lambda: run_prune(settings.prune_raw_min_age_days),
        seconds=settings.prune_raw_hours * 3600,
    )
    register(
        "finnhub_backfill",
        "finnhub_backfill",
        lambda: backfill_finnhub_news(
            settings.news_universe_file,
            backfill_days=settings.finnhub_backfill_days,
            chunk_days=settings.finnhub_backfill_chunk_days,
            max_requests=settings.finnhub_backfill_max_requests,
            request_spacing_seconds=settings.finnhub_request_spacing_seconds,
        ),
        seconds=settings.finnhub_backfill_hours * 3600,
        # Interval jobs first fire one interval after start; the backfill also fires on startup
        # (free-tier history rolls off daily, and a completed backfill makes this near-free).
        start_now=True,
    )
    register(
        "sentiment_score",
        "sentiment",
        run_sentiment_scoring,
        seconds=settings.score_sentiment_minutes * 60,
    )
    register(
        "prices_poll",
        "prices",
        lambda: poll_prices_job(settings),
        seconds=settings.poll_prices_hours * 3600,
        # Also fires on startup: bars go stale whenever the machine was off, and the
        # incremental fetch makes an already-current run nearly free.
        start_now=True,
    )
    job_health.write_artifact(artifact_path)
    return scheduler


def serve() -> None:  # pragma: no cover - blocking loop, exercised via build_scheduler in tests
    """Start the long-running scheduler (blocks). Ctrl-C to stop."""
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    # Before the startup jobs fire, not after: they get one shot each and a daily cadence, so
    # firing them against a database that is still booting costs a full day of data.
    if not wait_for_database(settings.db_wait_seconds):
        logger.error("serve: starting anyway; jobs will retry, but check the database")
    scheduler = build_scheduler(settings=settings)
    logger.info("serve: starting scheduler with jobs %s", [job.id for job in scheduler.get_jobs()])
    scheduler.start()
