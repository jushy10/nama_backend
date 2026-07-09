"""rename recommendation trends to analyst trends + add price targets

Renames ``stock_recommendation_trends`` → ``stock_analyst_trends`` (the slice now holds
broader analyst coverage, not just the buy/hold/sell trend) and adds the four consensus
price-target columns — ``target_mean`` / ``target_high`` / ``target_low`` / ``target_median``.
The targets are a single *current* snapshot, so the sync stamps them onto a stock's latest
monthly row only; all four are nullable (a stock without price-target coverage carries nulls,
and every existing row starts null). A pure rename otherwise — data, uniqueness, and the FK to
``stocks.id`` are untouched; the ``uq_recommendation_trends_stock_period`` constraint keeps its
name (a cosmetic legacy label, not worth a table rebuild to change). The add uses SQLite's
native ADD COLUMN; the downgrade drops via batch (table rebuild) so it works on SQLite too.

Revision ID: 0023_analyst_trends
Revises: 0022_analysis_cache
Create Date: 2026-07-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# (RDS) enforces the length even though SQLite ignores it, so a verbose id fails the deploy.
revision: str = "0023_analyst_trends"
down_revision: Union[str, None] = "0022_analysis_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TARGET_COLUMNS = ("target_mean", "target_high", "target_low", "target_median")


def upgrade() -> None:
    op.rename_table("stock_recommendation_trends", "stock_analyst_trends")
    for name in _TARGET_COLUMNS:
        op.add_column(
            "stock_analyst_trends", sa.Column(name, sa.Float(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("stock_analyst_trends") as batch_op:
        for name in reversed(_TARGET_COLUMNS):
            batch_op.drop_column(name)
    op.rename_table("stock_analyst_trends", "stock_recommendation_trends")
