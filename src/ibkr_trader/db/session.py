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


def engine_kwargs(url: URL) -> dict:
    """``create_engine`` keyword arguments for ``url``: server options on Postgres only."""
    kwargs: dict = {"pool_pre_ping": True}
    if url.get_backend_name() == "postgresql":
        kwargs["connect_args"] = {"options": TIMESCALE_OPTIONS}
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
