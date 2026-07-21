from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_stock_quarterly_earnings"
down_revision: Union[str, None] = "0002_stocks_analyst_estimates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_quarterly_earnings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stock_id", sa.Uuid(), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("fiscal_quarter", sa.Integer(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("report_date", sa.Date(), nullable=True),
        sa.Column("eps_actual", sa.Float(), nullable=True),
        sa.Column("eps_estimate", sa.Float(), nullable=True),
        sa.Column("eps_surprise", sa.Float(), nullable=True),
        sa.Column("eps_surprise_percent", sa.Float(), nullable=True),
        sa.Column("revenue_estimate", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stock_id",
            "fiscal_year",
            "fiscal_quarter",
            name="uq_quarterly_earnings_stock_period",
        ),
    )


def downgrade() -> None:
    op.drop_table("stock_quarterly_earnings")
