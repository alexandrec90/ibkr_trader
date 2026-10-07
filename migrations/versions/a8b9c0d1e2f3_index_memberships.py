"""index memberships (point-in-time S&P 500 universe)

Adds ``index_memberships``: one row per continuous span of a ticker's membership in an
index, filled by data_lake's ``ingestion.market.index_membership`` connector and priced by
``ingestion.market.index_pricing`` (``instrument_id`` / ``resolution`` / ``resolved_at``).
The backtest's point-in-time universe (``backtest.universe``) reads it so a run can ask
which companies were in the index on each decision date, not which survived to today.

Purely additive: a new table, no change to existing rows.

Revision ID: a8b9c0d1e2f3
Revises: f7b8c9d0e1f2
Create Date: 2026-10-06 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, None] = "f7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "index_memberships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("index_code", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("resolution", sa.String(length=64), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("index_code", "symbol", "start_date", "source"),
    )
    op.create_index(
        "ix_index_memberships_window",
        "index_memberships",
        ["index_code", "start_date", "end_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_index_memberships_window", table_name="index_memberships")
    op.drop_table("index_memberships")
