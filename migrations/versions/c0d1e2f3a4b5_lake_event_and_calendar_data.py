"""lake event and calendar data (splits, corporate events, macro, SEC XBRL facts)

Adopts the schema data-lake added in its PR #64 ("Add event and calendar data: earnings EPS,
splits, macro, central banks"):

- new tables ``stock_splits``, ``corporate_events``, ``macro_observations``,
  ``economic_events`` and ``sec_facts``;
- ``earnings_events`` gains the estimate/actual columns, ``hour``, ``first_seen_at`` and
  ``fetched_at`` (all nullable: rows written before them have none) and an index on
  ``report_ts``;
- ``ipo_events`` gains a nullable ``is_spac``.

Purely additive: new tables and nullable columns, no change to existing rows.

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-10-11 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, None] = "b9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Columns ``earnings_events`` gains, all nullable. Types, not Columns: a Column binds to the
#: first table it is added to, so each add_column builds a fresh one.
_EARNINGS_COLUMNS: dict[str, sa.types.TypeEngine] = {
    "eps_estimate": sa.Float(),
    "eps_actual": sa.Float(),
    "surprise_pct": sa.Float(),
    "revenue_estimate": sa.Float(),
    "revenue_actual": sa.Float(),
    "hour": sa.String(length=8),
    "first_seen_at": sa.DateTime(timezone=True),
    "fetched_at": sa.DateTime(timezone=True),
}


def upgrade() -> None:
    op.create_table(
        "stock_splits",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
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
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.Column("instrument_id", sa.Integer(), nullable=True),
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
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id"),
    )
    op.create_index(
        "ix_sec_facts_instrument_concept_period",
        "sec_facts",
        ["instrument_id", "concept", "period_end"],
    )

    for name, type_ in _EARNINGS_COLUMNS.items():
        op.add_column("earnings_events", sa.Column(name, type_, nullable=True))
    op.create_index("ix_earnings_events_report_ts", "earnings_events", ["report_ts"])

    op.add_column("ipo_events", sa.Column("is_spac", sa.Boolean(), nullable=True))


def downgrade() -> None:
    # batch_alter_table so the column drops also run on SQLite, which cannot DROP COLUMN
    # a table it has to rebuild; on Postgres it emits plain ALTERs.
    with op.batch_alter_table("ipo_events") as batch:
        batch.drop_column("is_spac")

    op.drop_index("ix_earnings_events_report_ts", table_name="earnings_events")
    with op.batch_alter_table("earnings_events") as batch:
        for name in reversed(_EARNINGS_COLUMNS):
            batch.drop_column(name)

    op.drop_index("ix_sec_facts_instrument_concept_period", table_name="sec_facts")
    op.drop_table("sec_facts")
    op.drop_index("ix_economic_events_event_date", table_name="economic_events")
    op.drop_table("economic_events")
    op.drop_index("ix_macro_observations_series_date", table_name="macro_observations")
    op.drop_table("macro_observations")
    op.drop_index("ix_corporate_events_event_date", table_name="corporate_events")
    op.drop_table("corporate_events")
    op.drop_table("stock_splits")
