"""yahoo corporate data (dividends, shares, statements, earnings)

Adds Instrument.sector/industry plus four tables populated by the Yahoo fundamentals
connector (ingestion.market.yahoo_fundamentals): dividends, share_counts,
fundamental_snapshots (snapshot-forward statements with never-updated first_seen), and
earnings_events (point-in-time lagging).

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-07-07 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("instruments", sa.Column("sector", sa.String(length=128), nullable=True))
    op.add_column("instruments", sa.Column("industry", sa.String(length=128), nullable=True))

    op.create_table(
        "dividends",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "ex_date", "source"),
    )

    op.create_table(
        "share_counts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("shares", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "date", "source"),
    )

    op.create_table(
        "fundamental_snapshots",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("freq", sa.String(length=16), nullable=False),
        sa.Column("statement", sa.String(length=16), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "freq", "statement", "period_end"),
    )

    op.create_table(
        "earnings_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("report_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "report_ts", "source"),
    )


def downgrade() -> None:
    op.drop_table("earnings_events")
    op.drop_table("fundamental_snapshots")
    op.drop_table("share_counts")
    op.drop_table("dividends")
    op.drop_column("instruments", "industry")
    op.drop_column("instruments", "sector")
