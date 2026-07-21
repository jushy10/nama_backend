from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007_recommendation_trends"
down_revision: Union[str, None] = "0006_drop_analyst_estimates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_recommendation_trends",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stock_id", sa.Uuid(), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("strong_buy", sa.Integer(), nullable=False),
        sa.Column("buy", sa.Integer(), nullable=False),
        sa.Column("hold", sa.Integer(), nullable=False),
        sa.Column("sell", sa.Integer(), nullable=False),
        sa.Column("strong_sell", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stock_id",
            "period",
            name="uq_recommendation_trends_stock_period",
        ),
    )


def downgrade() -> None:
    op.drop_table("stock_recommendation_trends")
