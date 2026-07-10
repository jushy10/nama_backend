"""add forward YoY growth to stocks

Adds the stock's latest *forward* year-over-year growth (percent) to the shared
``stocks`` anchor: ``forward_revenue_growth_yoy`` and ``forward_eps_growth_yoy`` — the
analyst-consensus FY1 -> FY2 change, the forward mirror of the trailing pair 0011 added
(feeding the universe search's forward-growth sorts and the AI analysis context). Like the
trailing snapshot, the annual-earnings slice overwrites these on every refresh, so a stock carries
just the current pair. Both nullable: unset until the annual slice has two *upcoming*
years cached (Yahoo often publishes only FY1, so this is frequently null). Batch mode so
the ADD/DROP works on SQLite too.

Revision ID: 0018_stocks_forward_growth
Revises: 0017_stock_pe
Create Date: 2026-07-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0018_stocks_forward_growth"
down_revision: Union[str, None] = "0017_stock_pe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("stocks") as batch_op:
        batch_op.add_column(
            sa.Column("forward_revenue_growth_yoy", sa.Float(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("forward_eps_growth_yoy", sa.Float(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("stocks") as batch_op:
        batch_op.drop_column("forward_eps_growth_yoy")
        batch_op.drop_column("forward_revenue_growth_yoy")
