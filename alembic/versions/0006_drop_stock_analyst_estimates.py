from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_drop_analyst_estimates"
down_revision: Union[str, None] = "0005_stock_annual_earnings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("stock_analyst_estimates")


def downgrade() -> None:
    # Recreates the table as 0002 defined it (rows are not restored — it was a
    # cache, refilled lazily by the code that used it).
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
