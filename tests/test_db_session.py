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


def test_postgres_closes_this_process_s_idle_connections():
    """2026-10-10: the db container (capped at 768m) was OOM-killed three times in 35
    minutes, and each kill reset every connection, so ``prices`` and ``social`` failed. The
    kernel's report showed four backends at 110-130 MB private memory each. One
    ``price_bars`` query leaves ~55 MB of catalog cache in its backend (790 chunks), and a
    pooled connection keeps that until it disconnects. ``pool_pre_ping`` replaces a
    connection the server closed, so callers never see the timeout."""
    kwargs = session_mod.engine_kwargs(make_url("postgresql+psycopg://u:p@db:5432/ibkr_trader"))
    options = kwargs["connect_args"]["options"]
    assert f"idle_session_timeout={session_mod.IDLE_SESSION_TIMEOUT_MS}" in options
    assert CAP in options
    assert kwargs["pool_pre_ping"] is True


def test_the_idle_timeout_outlasts_a_job_s_gap_between_sessions():
    """The yahoo/tiingo connectors open a session, download for up to a minute, then open
    another. A timeout shorter than that gap makes every symbol rebuild a cold backend,
    which took ~6 s for one cold ``price_bars`` query, against 0.6 s warm."""
    assert session_mod.IDLE_SESSION_TIMEOUT_MS >= 120_000


def test_non_postgres_engines_get_no_server_options():
    kwargs = session_mod.engine_kwargs(make_url("sqlite://"))
    assert "connect_args" not in kwargs
    assert kwargs["pool_pre_ping"] is True
