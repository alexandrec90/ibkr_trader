"""APScheduler wiring for `serve`: periodic ingestion polls + raw-pruning maintenance.

No trading here — the paper loop stays out until backtests justify it (see TODO §4). Each job
is wrapped by ``_guard`` so a single failure logs and the scheduler keeps running rather than
dying. Cadence and rate-limit spacing come from ``Settings`` (all off unless `serve` runs).
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import func, select

from ibkr_trader.config import Settings, get_settings
from ibkr_trader.db.models import Instrument, PriceBar
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
    from ibkr_trader.ingestion.social.google_trends import GoogleTrendsConnector

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
    from ibkr_trader.ingestion.social.google_trends import read_mapping_file

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
    from datetime import UTC, datetime, timedelta

    from ibkr_trader.ingestion.news.newsapi import (
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
    from ibkr_trader.ingestion.news.finnhub_news import FinnhubNewsConnector

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
    from ibkr_trader.ingestion.news.finnhub_backfill import run_backfill

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
    from ibkr_trader.ingestion.market.yahoo import YahooConnector
    from ibkr_trader.ingestion.market.yahoo_common import tracked_yahoo_symbols

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
    """Refresh daily FX bars from the newest stored bar forward (full history when empty)."""
    from datetime import timedelta

    from ibkr_trader.ingestion.market.fmp_fx import FmpFxConnector

    connector = FmpFxConnector()
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
            total += connector.fetch(pair=pair, date_from=date_from)
        except Exception:
            logger.exception("fx poll failed for %s", pair)
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
        _guard(lambda: poll_trends_job(settings), "trends"),
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
    scheduler.add_job(
        _guard(
            lambda: backfill_finnhub_news(
                settings.news_universe_file,
                backfill_days=settings.finnhub_backfill_days,
                chunk_days=settings.finnhub_backfill_chunk_days,
                max_requests=settings.finnhub_backfill_max_requests,
                request_spacing_seconds=settings.finnhub_request_spacing_seconds,
            ),
            "finnhub_backfill",
        ),
        "interval",
        hours=settings.finnhub_backfill_hours,
        # Interval jobs first fire one interval after start; the backfill also fires on startup
        # (free-tier history rolls off daily, and a completed backfill makes this near-free).
        next_run_time=datetime.now(UTC),
        id="finnhub_backfill",
    )
    scheduler.add_job(
        _guard(run_sentiment_scoring, "sentiment"),
        "interval",
        minutes=settings.score_sentiment_minutes,
        id="sentiment_score",
    )
    scheduler.add_job(
        _guard(lambda: poll_prices_job(settings), "prices"),
        "interval",
        hours=settings.poll_prices_hours,
        # Also fires on startup: bars go stale whenever the machine was off, and the
        # incremental fetch makes an already-current run nearly free.
        next_run_time=datetime.now(UTC),
        id="prices_poll",
    )
    return scheduler


def serve() -> None:  # pragma: no cover - blocking loop, exercised via build_scheduler in tests
    """Start the long-running scheduler (blocks). Ctrl-C to stop."""
    logging.basicConfig(level=logging.INFO)
    scheduler = build_scheduler()
    logger.info("serve: starting scheduler with jobs %s", [job.id for job in scheduler.get_jobs()])
    scheduler.start()
