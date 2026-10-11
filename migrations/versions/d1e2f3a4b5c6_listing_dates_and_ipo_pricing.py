"""listing dates + IPO pricing columns

Adds ``instruments.listed_on`` (the earliest date a security is known to have traded, a lower
bound on its age; NULL when unknown) and the pricing columns of ``ipo_events``
(``instrument_id`` / ``resolution`` / ``resolved_at``, the same trio ``index_memberships``
carries). Filled by data_lake's IPO pricing; read by ``backtest.universe.with_ipo_cohort``
(the IPO cohort a model trains on) and ``signals.dataset._age_unknown`` (whether a series'
first bar is its listing).

Purely additive: nullable columns, no change to existing rows.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-10-10 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c0d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK = "fk_ipo_events_instrument_id_instruments"


def upgrade() -> None:
    op.add_column("instruments", sa.Column("listed_on", sa.Date(), nullable=True))
    # batch mode: a plain ALTER on Postgres, a table copy on SQLite (which cannot ALTER in a
    # foreign key) -- so the migration test runs the same file production does
    with op.batch_alter_table("ipo_events") as batch:
        batch.add_column(sa.Column("instrument_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("resolution", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(_FK, "instruments", ["instrument_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("ipo_events") as batch:
        batch.drop_constraint(_FK, type_="foreignkey")
        batch.drop_column("resolved_at")
        batch.drop_column("resolution")
        batch.drop_column("instrument_id")
    op.drop_column("instruments", "listed_on")
