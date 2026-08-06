"""features table (versioned per-day feature snapshots)

Adds the `features` table written by signals.features.build_daily_features: one JSONB
payload per (instrument, day, feature_set_version) so training and backtests share
identical, versioned inputs.

Revision ID: d4e5f6a7b8c9
Revises: c2d3e4f5a6b7
Create Date: 2026-07-08 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "features",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("feature_set_version", sa.String(length=16), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_id", "ts", "feature_set_version"),
    )


def downgrade() -> None:
    op.drop_table("features")
