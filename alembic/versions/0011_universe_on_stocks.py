"""universe columns on stocks

The searchable ≥$1B universe is folded straight into the ``stocks`` anchor rather than a
separate membership table: this adds the three screen facts the universe sync fills —
``sector``, ``market_cap`` and ``screened_at`` (the last-screen stamp). All nullable: a
stock that reached ``stocks`` some other way (a ticker-card lookup, an earnings refresh)
simply has them null, and search treats ``market_cap IS NOT NULL`` as "is a screened
member". The ``stocks`` table already exists (created in 0002), so this only alters it.

Revision ID: 0011_universe_on_stocks
Revises: 0010_stocks_ticker
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0011_universe_on_stocks"
down_revision: Union[str, None] = "0010_stocks_ticker"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stocks", sa.Column("sector", sa.String(length=64), nullable=True))
    op.add_column("stocks", sa.Column("market_cap", sa.Float(), nullable=True))
    op.add_column(
        "stocks",
        sa.Column("screened_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stocks", "screened_at")
    op.drop_column("stocks", "market_cap")
    op.drop_column("stocks", "sector")
