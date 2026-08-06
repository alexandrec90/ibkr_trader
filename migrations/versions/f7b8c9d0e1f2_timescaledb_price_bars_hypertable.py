"""timescaledb hypertable + compression for price_bars

Converts ``price_bars`` into a TimescaleDB hypertable partitioned on ``ts`` (~1 month chunks)
with native columnar compression (segment by instrument_id, bar_size; order by ts) and a policy
that compresses chunks older than 7 days. Recent chunks stay uncompressed so the read-then-write
bar upsert path stays cheap; Timescale transparently decompresses older chunks on write.

TOOLS-06: justified once the service began growing toward retained intraday bars. See
docs/plans/completed/tools-06-timescaledb.md.

The whole migration is **guarded** and does nothing unless the bind is Postgres *and* the
TimescaleDB extension is actually available in the image:

* SQLite (the test database) — hypertables are Postgres-only, so this is a clean no-op and the
  ORM keeps ``price_bars`` as an ordinary table.
* Plain ``postgres:16`` (e.g. a CI Postgres without the extension) — also a no-op, so
  environments that never switch to the ``timescaledb`` image stay green.

A hypertable requires the partitioning column (``ts``) in every unique/PK constraint. The ORM
model still declares ``id`` as the sole primary key (SQLite needs a single-column integer PK for
autoincrement, and Alembic autogenerate does not diff primary keys); here, on Postgres+Timescale
only, we widen the real primary key to ``(id, ts)``. ``id`` remains globally unique via its
sequence, so ORM identity and UPDATE-by-id are unaffected.

Revision ID: f7b8c9d0e1f2
Revises: a7b8c9d0e1f2
Create Date: 2026-07-19 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f7b8c9d0e1f2"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timescale_available() -> bool:
    """True only on a Postgres bind whose image ships the timescaledb extension."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return False
    return bool(
        bind.execute(
            sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
        ).scalar()
    )


def upgrade() -> None:
    if not _timescale_available():
        return

    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    # Widen the primary key to include the partitioning column. Every other constraint on the
    # table already includes ``ts`` (the (instrument_id, ts, bar_size, source, what_to_show)
    # unique key and the (instrument_id, ts) index), so this is the only blocker to conversion.
    op.execute("ALTER TABLE price_bars DROP CONSTRAINT price_bars_pkey")
    op.execute("ALTER TABLE price_bars ADD CONSTRAINT price_bars_pkey PRIMARY KEY (id, ts)")

    # Convert in place, migrating existing rows into ~1 month chunks. Idempotent so a re-run
    # (or a fresh volume that somehow already converted) does not error.
    op.execute(
        "SELECT create_hypertable('price_bars', by_range('ts', INTERVAL '1 month'), "
        "migrate_data => true, if_not_exists => true)"
    )

    # Columnar compression: group each stored series (instrument + bar size) together and order
    # by time so OHLCV columns compress well (~90% on daily bars).
    op.execute(
        "ALTER TABLE price_bars SET ("
        "timescaledb.compress, "
        "timescaledb.compress_segmentby = 'instrument_id, bar_size', "
        "timescaledb.compress_orderby = 'ts DESC'"
        ")"
    )

    # Compress chunks older than 7 days; the most recent (hot) chunk stays row-oriented so
    # daily upserts and same-day corrections never pay decompression cost.
    op.execute(
        "SELECT add_compression_policy('price_bars', INTERVAL '7 days', if_not_exists => true)"
    )


def downgrade() -> None:
    if not _timescale_available():
        return

    # Reverse compression only. Un-hypertabling a table in place is not supported by
    # TimescaleDB (it would require recreating the table and copying rows); restore from a
    # pre-migration pg_dump if a plain table is truly needed. The composite primary key is also
    # left in place because a hypertable cannot drop the partitioning column from its PK.
    op.execute("SELECT remove_compression_policy('price_bars', if_exists => true)")
    op.execute("SELECT decompress_chunk(c, true) FROM show_chunks('price_bars') c")
    op.execute("ALTER TABLE price_bars SET (timescaledb.compress = false)")
