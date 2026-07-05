"""drop index_constituents

Removes the legacy ``index_constituents`` table (created by 0001). The stock
screener it backed has been removed, and index membership now lives on the
``stocks`` anchor as the ``in_sp500`` / ``in_nasdaq100`` flags (0014), reconciled
by the index-membership sync. The table is unused, so drop it. ``downgrade``
recreates the empty table (mirroring 0001) for reversibility.

Revision ID: 0015_drop_index_constituents
Revises: 0014_index_flags_on_stocks
Create Date: 2026-07-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0015_drop_index_constituents"
down_revision: Union[str, None] = "0014_index_flags_on_stocks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("index_constituents")


def downgrade() -> None:
    op.create_table(
        "index_constituents",
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("sector", sa.String(length=64), nullable=True),
        sa.Column("in_sp500", sa.Boolean(), nullable=False),
        sa.Column("in_nasdaq100", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("symbol"),
    )
