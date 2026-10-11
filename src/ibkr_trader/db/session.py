"""Engine/session factory. One engine per process; sessions are short-lived."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import URL, Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from ibkr_trader.config import get_settings

#: Lift TimescaleDB's per-transaction decompression cap (default 100,000 rows). ``price_bars``
#: chunks older than 7 days are compressed, and writing history into them — a new ticker's
#: bars from 2008, a delisted S&P 500 member from Tiingo — decompresses those chunks. With the
#: cap, the first point-in-time backfill died on its first instrument (100,829 rows). The
#: compression policy recompresses the touched chunks afterwards. A custom ``timescaledb.*``
#: option is accepted by a Postgres without the extension too, so this is safe everywhere.
TIMESCALE_OPTIONS = "-c timescaledb.max_tuples_decompressed_per_dml_transaction=0"

#: The server closes this process's connections after 5 idle minutes. A backend keeps every
#: catalog entry it has loaded until it disconnects, and one ``price_bars`` query loads ~55 MB
#: (790 chunks and their indexes). The pool holds up to five connections, and each one
#: kept that cache, so on 2026-10-10 the 768m db container was OOM-killed three times in 35
#: minutes with four backends at 110-130 MB each. ``pool_pre_ping`` replaces a connection
#: the server closed, so a caller never sees the timeout. The limit is far longer than the
#: gap between one job's sessions (a 45 s Yahoo download), so a running job keeps its warm
#: backend. Needs Postgres 14+.
IDLE_SESSION_TIMEOUT_MS = 300_000

#: Plan every statement with its parameters' values. psycopg prepares a statement from its
#: fifth run, and Postgres then tries a generic plan, which cannot exclude ``price_bars``
#: chunks by a ``ts`` parameter, so it opens all ~790 of them. Measured 2026-10-11 on the live
#: db: a 45-day-bounded ``max(ts)`` held its backend at 2 MB for ten runs and jumped to 79 MB
#: on the eleventh; with this option it stayed at 2 MB. Every bounded query a job repeats per
#: symbol (the upsert's preload, the newest-bar lookups) is one of those. The idle timeout
#: above frees that cache only once a job stops; this keeps a running job from loading it.
#: Re-planning costs a few ms against a chunk-excluded plan.
PLAN_CACHE_OPTION = "-c plan_cache_mode=force_custom_plan"

POSTGRES_OPTIONS = (
    f"{TIMESCALE_OPTIONS} -c idle_session_timeout={IDLE_SESSION_TIMEOUT_MS} {PLAN_CACHE_OPTION}"
)


def engine_kwargs(url: URL) -> dict:
    """``create_engine`` keyword arguments for ``url``: server options on Postgres only."""
    kwargs: dict = {"pool_pre_ping": True}
    if url.get_backend_name() == "postgresql":
        kwargs["connect_args"] = {"options": POSTGRES_OPTIONS}
    return kwargs


@lru_cache
def get_engine() -> Engine:
    url = make_url(get_settings().database_url)
    return create_engine(url, **engine_kwargs(url))


@contextmanager
def get_session() -> Iterator[Session]:
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
