"""add revenue_actual to stock_quarterly_earnings

Adds the reported-revenue column to the quarterly-earnings cache. Like the rest of the row
it's sourced from yfinance (the quarterly income statement's Total Revenue) and is nullable
— only reported quarters carry it. Batch mode so the ADD/DROP works on SQLite too.

Revision ID: 0004_quarterly_earnings_revenue_actual
Revises: 0003_stock_quarterly_earnings
Create Date: 2026-07-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_quarterly_earnings_revenue_actual"
down_revision: Union[str, None] = "0003_stock_quarterly_earnings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("stock_quarterly_earnings") as batch_op:
        batch_op.add_column(sa.Column("revenue_actual", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("stock_quarterly_earnings") as batch_op:
        batch_op.drop_column("revenue_actual")
