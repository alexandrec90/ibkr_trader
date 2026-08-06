"""forward strategy snapshots

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-10 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strategy_snapshots",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("model_version", sa.String(length=32), nullable=False),
        sa.Column("feature_set_version", sa.String(length=16), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("weights", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy", "ts"),
    )


def downgrade() -> None:
    op.drop_table("strategy_snapshots")
