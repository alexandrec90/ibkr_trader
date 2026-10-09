"""ipo events (IPO calendar deals + EDGAR registration filings)

Adds ``ipo_events``: one row per EDGAR registration filing (S-1/F-1/DRS and amendments,
424B4, RW -- keyed by accession number) or per Finnhub IPO-calendar deal (keyed by a hash of
the normalised company name, so a re-dated deal updates in place). Filled by data_lake's
``ingestion.market.sec_edgar`` and ``ingestion.market.finnhub_ipo`` connectors; read by
``ibkr_trader.ipo_watch`` for the major-IPO phone alert.

Purely additive: a new table, no change to existing rows.

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-10-08 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b9c0d1e2f3a4"
down_revision: Union[str, None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ipo_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=True),
        sa.Column("exchange", sa.String(length=32), nullable=True),
        sa.Column("cik", sa.String(length=10), nullable=True),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("form_type", sa.String(length=16), nullable=True),
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_date", sa.Date(), nullable=True),
        sa.Column("price_low", sa.Float(), nullable=True),
        sa.Column("price_high", sa.Float(), nullable=True),
        sa.Column("shares", sa.BigInteger(), nullable=True),
        sa.Column("deal_value_usd", sa.Float(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id"),
    )
    op.create_index("ix_ipo_events_expected_date", "ipo_events", ["expected_date"])
    op.create_index("ix_ipo_events_filed_at", "ipo_events", ["filed_at"])


def downgrade() -> None:
    op.drop_index("ix_ipo_events_filed_at", table_name="ipo_events")
    op.drop_index("ix_ipo_events_expected_date", table_name="ipo_events")
    op.drop_table("ipo_events")
