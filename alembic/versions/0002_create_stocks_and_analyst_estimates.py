"""create stocks and stock_analyst_estimates

The analyst-estimates cache: a thin ``stocks`` anchor (UUID id, unique symbol,
optional name) plus a one-row-per-stock ``stock_analyst_estimates`` table holding the
current forward consensus. Mirrors app.stocks.stocks.models (the `stocks` anchor) +
app.stocks.estimates.models (`stock_analyst_estimates`). Filled lazily on a miss and
refreshed by the estimates cron endpoint (Yahoo -> DB), so both start empty.

Revision ID: 0002_stocks_analyst_estimates
Revises: 0001_index_constituents
Create Date: 2026-06-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_stocks_analyst_estimates"
down_revision: Union[str, None] = "0001_index_constituents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stocks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol"),
    )
    op.create_table(
        "stock_analyst_estimates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stock_id", sa.Uuid(), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("eps_avg", sa.Float(), nullable=True),
        sa.Column("eps_low", sa.Float(), nullable=True),
        sa.Column("eps_high", sa.Float(), nullable=True),
        sa.Column("revenue_avg", sa.Float(), nullable=True),
        sa.Column("num_analysts_eps", sa.Integer(), nullable=True),
        sa.Column("num_analysts_revenue", sa.Integer(), nullable=True),
        sa.Column("fiscal_year_fy2", sa.Integer(), nullable=True),
        sa.Column("eps_avg_fy2", sa.Float(), nullable=True),
        sa.Column("revenue_avg_fy2", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stock_id"),
    )


def downgrade() -> None:
    op.drop_table("stock_analyst_estimates")
    op.drop_table("stocks")
