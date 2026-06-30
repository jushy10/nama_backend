"""create stock_company_profile

The company-profile cache's description table: one row per stock holding the business
description (the name rides the shared ``stocks`` anchor from 0002). Mirrors
app.stocks.stock_profile_repository.StockCompanyProfileRecord. Filled lazily on a miss
and refreshed by scripts/sync_profiles.py, so it starts empty.

Revision ID: 0003_stock_company_profile
Revises: 0002_stocks_analyst_estimates
Create Date: 2026-06-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_stock_company_profile"
down_revision: Union[str, None] = "0002_stocks_analyst_estimates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_company_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stock_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stock_id"),
    )


def downgrade() -> None:
    op.drop_table("stock_company_profile")
