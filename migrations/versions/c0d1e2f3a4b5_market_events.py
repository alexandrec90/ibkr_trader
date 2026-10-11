"""market events: earnings EPS, splits, announced dividends, macro, economic calendar, buybacks

Adds what data_lake's event connectors write:

- ``earnings_events`` gains EPS estimate/actual/surprise, revenue estimate/actual, the
  report hour (bmo/amc), ``first_seen_at`` and ``fetched_at``, plus an index on
  ``report_ts`` for calendar reads. All nullable: existing rows predate them.
- ``stock_splits`` -- split history (Yahoo), ratio = new shares per old share.
- ``corporate_events`` -- announced upcoming per-instrument dates (next ex-dividend and
  dividend payment, Yahoo's calendar).
- ``macro_observations`` -- FRED and Bank of Canada series (rates, CPI, unemployment, ...).
- ``economic_events`` -- scheduled macro events: FRED data releases, FOMC meetings, Bank of
  Canada rate announcements, past and upcoming.
- ``sec_facts`` -- SEC XBRL company facts; the buyback concepts by default.
- ``ipo_events.is_spac`` -- SIC 6770 ("blank checks"). Backfilled on Postgres from the SIC
  the EDGAR connector already kept in ``raw``; NULL where no SIC is known.

Additive only: new tables and nullable columns.

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-10-10 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, None] = "b9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _stamps() -> list[sa.Column]:
    return [
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    with op.batch_alter_table("earnings_events") as batch:
        batch.add_column(sa.Column("eps_estimate", sa.Float(), nullable=True))
        batch.add_column(sa.Column("eps_actual", sa.Float(), nullable=True))
        batch.add_column(sa.Column("surprise_pct", sa.Float(), nullable=True))
        batch.add_column(sa.Column("revenue_estimate", sa.Float(), nullable=True))
        batch.add_column(sa.Column("revenue_actual", sa.Float(), nullable=True))
        batch.add_column(sa.Column("hour", sa.String(length=8), nullable=True))
        batch.add_column(sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_earnings_events_report_ts", "earnings_events", ["report_ts"])

    op.create_table(
        "stock_splits",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("ratio", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "ex_date", "source"),
    )

    op.create_table(
        "corporate_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "kind", "event_date", "source"),
    )
    op.create_index("ix_corporate_events_event_date", "corporate_events", ["event_date"])

    op.create_table(
        "macro_observations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("series_id", sa.String(length=64), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        *_stamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "series_id", "date"),
    )
    op.create_index(
        "ix_macro_observations_series_date", "macro_observations", ["series_id", "date"]
    )

    op.create_table(
        "economic_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("release_id", sa.Integer(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id"),
    )
    op.create_index("ix_economic_events_event_date", "economic_events", ["event_date"])

    op.create_table(
        "sec_facts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=False),
        sa.Column("cik", sa.String(length=10), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=True),
        sa.Column("taxonomy", sa.String(length=16), nullable=False),
        sa.Column("concept", sa.String(length=128), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("fiscal_period", sa.String(length=4), nullable=True),
        sa.Column("form", sa.String(length=16), nullable=False),
        sa.Column("filed", sa.Date(), nullable=False),
        sa.Column("frame", sa.String(length=16), nullable=True),
        *_stamps(),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id"),
    )
    op.create_index(
        "ix_sec_facts_instrument_concept_period",
        "sec_facts",
        ["instrument_id", "concept", "period_end"],
    )

    with op.batch_alter_table("ipo_events") as batch:
        batch.add_column(sa.Column("is_spac", sa.Boolean(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        # The EDGAR connector has always kept the filer's SIC in raw; 6770 is "blank checks".
        op.execute(
            "UPDATE ipo_events SET is_spac = (raw->>'sic' = '6770') "
            "WHERE source = 'sec_edgar' AND raw->>'sic' IS NOT NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("ipo_events") as batch:
        batch.drop_column("is_spac")
    op.drop_index("ix_sec_facts_instrument_concept_period", table_name="sec_facts")
    op.drop_table("sec_facts")
    op.drop_index("ix_economic_events_event_date", table_name="economic_events")
    op.drop_table("economic_events")
    op.drop_index("ix_macro_observations_series_date", table_name="macro_observations")
    op.drop_table("macro_observations")
    op.drop_index("ix_corporate_events_event_date", table_name="corporate_events")
    op.drop_table("corporate_events")
    op.drop_table("stock_splits")
    op.drop_index("ix_earnings_events_report_ts", table_name="earnings_events")
    with op.batch_alter_table("earnings_events") as batch:
        for column in (
            "fetched_at",
            "first_seen_at",
            "hour",
            "revenue_actual",
            "revenue_estimate",
            "surprise_pct",
            "eps_actual",
            "eps_estimate",
        ):
            batch.drop_column(column)
