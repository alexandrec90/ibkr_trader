"""Engine options: what every connection this process opens tells the server."""

from sqlalchemy.engine import make_url

from ibkr_trader.db import session as session_mod

CAP = "timescaledb.max_tuples_decompressed_per_dml_transaction=0"


def test_postgres_connections_lift_the_timescale_decompression_cap():
    """2026-10-07: the first point-in-time backfill died on its first instrument with
    "tuple decompression limit exceeded by operation (current limit: 100000, tuples
    decompressed: 100829)". ``price_bars`` chunks older than 7 days are compressed, and
    writing a new ticker's history from 2008 decompressed every row in those chunks. Any
    deep backfill of a new ticker (``ingest prices``, Tiingo's dead members) hits it."""
    kwargs = session_mod.engine_kwargs(make_url("postgresql+psycopg://u:p@db:5432/ibkr_trader"))
    assert CAP in kwargs["connect_args"]["options"]
    assert kwargs["pool_pre_ping"] is True


def test_non_postgres_engines_get_no_server_options():
    kwargs = session_mod.engine_kwargs(make_url("sqlite://"))
    assert "connect_args" not in kwargs
    assert kwargs["pool_pre_ping"] is True
