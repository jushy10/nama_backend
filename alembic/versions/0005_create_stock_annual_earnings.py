"""create stock_annual_earnings

The annual-earnings cache: a time-series child of the ``stocks`` anchor holding a stock's
recent reported fiscal years and its upcoming (estimated) ones — many rows per stock, one
per fiscal year, unique on ``(stock_id, fiscal_year)``. Mirrors
app.stocks.earnings.annual.models. Filled lazily on a miss and refreshed by the
annual-earnings cron endpoint (yfinance -> DB), so it starts empty. The ``stocks`` anchor
already exists (created in 0002), so this migration only adds the child table.

Revision ID: 0005_stock_annual_earnings
Revises: 0004_quarterly_revenue_actual
Create Date: 2026-07-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0005_stock_annual_earnings"
down_revision: Union[str, None] = "0004_quarterly_revenue_actual"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_annual_earnings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stock_id", sa.Uuid(), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("eps_actual", sa.Float(), nullable=True),
        sa.Column("eps_estimate", sa.Float(), nullable=True),
        sa.Column("revenue_actual", sa.Float(), nullable=True),
        sa.Column("revenue_estimate", sa.Float(), nullable=True),
        sa.Column("net_income", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stock_id",
            "fiscal_year",
            name="uq_annual_earnings_stock_year",
        ),
    )


def downgrade() -> None:
    op.drop_table("stock_annual_earnings")
