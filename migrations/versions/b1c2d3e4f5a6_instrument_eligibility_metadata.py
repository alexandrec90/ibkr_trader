"""instrument eligibility metadata

Adds asset_class + leveraged flags to instruments, consumed by signals.eligibility to exclude
leveraged/inverse ETPs and to distinguish ETFs from stocks (IBKR sec_type is "STK" for both).

Revision ID: b1c2d3e4f5a6
Revises: 8f0f7d286aa6
Create Date: 2026-07-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = '8f0f7d286aa6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('instruments', sa.Column('asset_class', sa.String(length=8), nullable=True))
    op.add_column('instruments', sa.Column('leveraged', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('instruments', 'leveraged')
    op.drop_column('instruments', 'asset_class')
