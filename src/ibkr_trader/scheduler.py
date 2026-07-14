"""APScheduler wiring for `serve`: periodic ingestion polls + raw-pruning maintenance.

No trading here — the paper loop stays out until backtests justify it (see TODO §4). Each job
is wrapped by ``_guard`` so a single failure logs and the scheduler keeps running rather than
dying. Cadence and rate-limit spacing come from ``Settings`` (all off unless `serve` runs).
"""

import logging
import time
from collections.abc import Callable

from apscheduler.schedulers.blocking import BlockingScheduler

from ibkr_trader.config import Settings, get_settings
from ibkr_trader.db.session import get_session
from ibkr_trader.maintenance import prune_scored_raw

logger = logging.getLogger(__name__)


def _read_universe_file(path: str) -> list[str]:
    try:
        with open(path) as handle:
            return [line.strip().upper() for line in handle if line.strip()]
    except OSError:
        logger.warning("universe file %r not readable; finnhub-news poll is a no-op", path)
        return []


def poll_reddit() -> int:
    from ibkr_trader.ingestion.social.reddit import RedditConnector

    count = RedditConnector().fetch()
    logger.info("reddit poll upserted %d posts", count)
    return count


def poll_trends(keywords: list[str]) -> int:
    if not keywords:
        logger.info("trends poll skipped: no trends_keywords configured")
        return 0
    from ibkr_trader.ingestion.social.google_trends import GoogleTrendsConnector

    count = GoogleTrendsConnector().fetch(keywords=keywords)
    logger.info("trends poll upserted %d points", count)
    return count


def poll_finnhub_news(
    universe_file: str,
    request_spacing_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Fetch company news for every universe symbol, spacing calls under the free-tier limit.

    A per-symbol failure is logged and skipped so one bad ticker doesn't abort the whole poll.
    """
    from ibkr_trader.ingestion.news.finnhub_news import FinnhubNewsConnector

    symbols = _read_universe_file(universe_file)
    connector = FinnhubNewsConnector()
    total = 0
    for index, symbol in enumerate(symbols):
        if index:
            sleep(request_spacing_seconds)
        try:
            total += connector.fetch(symbol=symbol)
        except Exception:
            logger.exception("finnhub-news poll failed for %s", symbol)
    logger.info("finnhub-news poll upserted %d articles across %d symbols", total, len(symbols))
    return total


def run_prune(min_age_days: int) -> dict[str, int]:
    with get_session() as session:
        counts = prune_scored_raw(session, min_age_days=min_age_days)
    logger.info("prune dropped raw payloads: %s", counts)
    return counts


def _guard(job: Callable[[], object], label: str) -> Callable[[], None]:
    """Wrap a job so an exception is logged, not propagated (keeps the scheduler alive)."""

    def wrapped() -> None:
        try:
            job()
        except Exception:
            logger.exception("scheduled job %r failed", label)

    return wrapped


def build_scheduler(
    settings: Settings | None = None,
    scheduler: BlockingScheduler | None = None,
) -> BlockingScheduler:
    """Register the periodic jobs on a scheduler and return it (unstarted, so it's testable)."""
    settings = settings or get_settings()
    scheduler = scheduler or BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        _guard(poll_reddit, "reddit"),
        "interval",
        minutes=settings.poll_reddit_minutes,
        id="reddit_poll",
    )
    scheduler.add_job(
        _guard(
            lambda: poll_finnhub_news(
                settings.news_universe_file, settings.finnhub_request_spacing_seconds
            ),
            "finnhub_news",
        ),
        "interval",
        hours=settings.poll_finnhub_news_hours,
        id="finnhub_news_poll",
    )
    scheduler.add_job(
        _guard(lambda: poll_trends(settings.trends_keywords), "trends"),
        "interval",
        hours=settings.poll_trends_hours,
        id="trends_poll",
    )
    scheduler.add_job(
        _guard(lambda: run_prune(settings.prune_raw_min_age_days), "prune"),
        "interval",
        hours=settings.prune_raw_hours,
        id="prune_raw",
    )
    return scheduler


def serve() -> None:  # pragma: no cover - blocking loop, exercised via build_scheduler in tests
    """Start the long-running scheduler (blocks). Ctrl-C to stop."""
    logging.basicConfig(level=logging.INFO)
    scheduler = build_scheduler()
    logger.info("serve: starting scheduler with jobs %s", [job.id for job in scheduler.get_jobs()])
    scheduler.start()
