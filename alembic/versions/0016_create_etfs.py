"""create etfs

The top-ETFs cache: a standalone table (not a child of the ``stocks`` anchor — an ETF is not a
company, so it gets its own table rather than a ``stocks`` row that would leak funds into the
stock universe search) holding the screened top US ETF set — one row per fund, unique on
``ticker``. Filled and refreshed by the ETF cron endpoint (yfinance ``top_etfs_us`` -> DB), so
it starts empty. Mirrors app.stocks.etfs.models.

Revision ID: 0016_create_etfs
Revises: 0015_drop_index_constituents
Create Date: 2026-07-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0016_create_etfs"
down_revision: Union[str, None] = "0015_drop_index_constituents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "etfs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("exchange", sa.String(length=32), nullable=True),
        sa.Column("net_assets", sa.Float(), nullable=True),
        sa.Column("expense_ratio", sa.Float(), nullable=True),
        sa.Column("ytd_return", sa.Float(), nullable=True),
        sa.Column("screened_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticker", name="uq_etfs_ticker"),
    )


def downgrade() -> None:
    op.drop_table("etfs")
