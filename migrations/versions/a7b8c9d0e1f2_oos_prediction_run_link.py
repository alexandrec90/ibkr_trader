"""Link OOS predictions to the backtest run that produced them.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("predictions", sa.Column("backtest_run_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_predictions_backtest_run_id",
        "predictions",
        "backtest_runs",
        ["backtest_run_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_predictions_backtest_run_instrument_ts",
        "predictions",
        ["backtest_run_id", "instrument_id", "ts"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_predictions_backtest_run_instrument_ts", "predictions", type_="unique")
    op.drop_constraint("fk_predictions_backtest_run_id", "predictions", type_="foreignkey")
    op.drop_column("predictions", "backtest_run_id")
