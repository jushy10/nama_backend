"""create stock_analyst_rating_changes

The sibling of ``stock_analyst_trends`` for the discrete upgrade/downgrade *events* behind
the monthly trend — a time-series child of the ``stocks`` anchor, one row per firm action,
unique on ``(stock_id, firm, published_at)``. Mirrors
app.stocks.recommendations.models.StockAnalystRatingChangeRecord. Filled by the same
recommendations sweep (yfinance → DB), insert-only (each event is a frozen fact), so it
accumulates a longer history than Yahoo serves at once and starts empty. ``firm`` and
``published_at`` are non-null (the identity); grades, action label, and price targets are
nullable. The ``stocks`` anchor already exists (created in 0002), so this only adds the child.

Revision ID: 0024_analyst_rating_changes
Revises: 0023_analyst_trends
Create Date: 2026-07-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# (RDS) enforces the length even though SQLite ignores it, so a verbose id fails the deploy.
revision: str = "0024_analyst_rating_changes"
down_revision: Union[str, None] = "0023_analyst_trends"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_analyst_rating_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stock_id", sa.Uuid(), nullable=False),
        sa.Column("firm", sa.String(length=128), nullable=False),
        sa.Column("published_at", sa.Date(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=True),
        sa.Column("from_grade", sa.String(length=64), nullable=True),
        sa.Column("to_grade", sa.String(length=64), nullable=True),
        sa.Column("target_current", sa.Float(), nullable=True),
        sa.Column("target_prior", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stock_id",
            "firm",
            "published_at",
            name="uq_analyst_rating_changes_stock_firm_date",
        ),
    )


def downgrade() -> None:
    op.drop_table("stock_analyst_rating_changes")
