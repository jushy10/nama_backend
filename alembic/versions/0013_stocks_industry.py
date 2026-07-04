"""industry column on stocks

Adds ``industry`` to the ``stocks`` anchor, beside the ``sector`` column migration 0012
added. Both are filled by the universe sync's enrichment pass from Yahoo's per-ticker
``.info`` (the bulk screen carries neither), as snake_case slugs. Nullable, like the other
anchor facts: a stock reaches the table with it unset and the enrichment fills it once.
The ``stocks`` table already exists (created in 0002), so this only alters it.

Revision ID: 0013_stocks_industry
Revises: 0012_universe_on_stocks
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0013_stocks_industry"
down_revision: Union[str, None] = "0012_universe_on_stocks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stocks", sa.Column("industry", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("stocks", "industry")
