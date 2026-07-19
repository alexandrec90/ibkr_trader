"""sentiment model provenance

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-18 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("news_articles", sa.Column("sentiment_model", sa.String(32), nullable=True))
    op.add_column("social_posts", sa.Column("sentiment_model", sa.String(32), nullable=True))
    op.execute(
        sa.text("UPDATE news_articles SET sentiment_model = 'vader' WHERE sentiment IS NOT NULL")
    )
    op.execute(
        sa.text("UPDATE social_posts SET sentiment_model = 'vader' WHERE sentiment IS NOT NULL")
    )


def downgrade() -> None:
    op.drop_column("social_posts", "sentiment_model")
    op.drop_column("news_articles", "sentiment_model")
