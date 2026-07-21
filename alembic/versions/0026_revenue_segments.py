from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres (RDS)
# enforces the length even though SQLite ignores it, so a verbose id fails the deploy.
revision: str = "0026_revenue_segments"
down_revision: Union[str, None] = "0025_analyst_rating_changes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_revenue_segments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stock_id", sa.Uuid(), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("axis", sa.String(length=32), nullable=False),
        sa.Column("member", sa.String(length=160), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stock_id",
            "fiscal_year",
            "axis",
            "member",
            name="uq_revenue_segments_stock_year_axis_member",
        ),
    )


def downgrade() -> None:
    op.drop_table("stock_revenue_segments")
