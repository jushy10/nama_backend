"""create stock_universe

The searchable-universe membership: a 1:1 child of the ``stocks`` anchor marking a stock as
currently in the screened US ≥$5B set, carrying its drifting ``market_cap`` / ``sector`` and
the ``screened_at`` stamp of the last screen that included it. Mirrors
app.stocks.universe.models. Populated out of band by the universe cron endpoint (Nasdaq
screen -> DB), so it starts empty; ``stocks`` anchor rows are created / filled alongside.
The ``stocks`` anchor already exists (created in 0002), so this migration only adds the
child table.

Revision ID: 0011_stock_universe
Revises: 0010_stocks_ticker
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0011_stock_universe"
down_revision: Union[str, None] = "0010_stocks_ticker"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_universe",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stock_id", sa.Uuid(), nullable=False),
        sa.Column("market_cap", sa.Float(), nullable=True),
        sa.Column("sector", sa.String(length=64), nullable=True),
        sa.Column("screened_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stock_id", name="uq_stock_universe_stock"),
    )


def downgrade() -> None:
    op.drop_table("stock_universe")
