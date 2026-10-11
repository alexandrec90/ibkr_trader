"""backfill ipo_events.is_spac from the SIC the EDGAR connector kept in raw

c0d1e2f3a4b5 added the nullable ``is_spac`` column; rows written before it have none. The
EDGAR connector has always kept the filer's SIC in ``raw``, and SIC 6770 is "blank checks",
so those rows can be labelled. NULL stays NULL where no SIC is known.

Postgres only: ``raw->>'sic'`` is its JSON operator, and SQLite holds only test databases.
Data only, so the downgrade has nothing to undo -- c0d1e2f3a4b5's downgrade drops the column.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-10-11 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c0d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BACKFILL = (
    "UPDATE ipo_events SET is_spac = (raw->>'sic' = '6770') "
    "WHERE source = 'sec_edgar' AND raw->>'sic' IS NOT NULL AND is_spac IS NULL"
)


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(BACKFILL)


def downgrade() -> None:
    pass
