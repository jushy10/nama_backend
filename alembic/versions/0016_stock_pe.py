"""pe_ratio column on stocks

Adds ``pe_ratio`` to the ``stocks`` anchor — the stock's trailing P/E on the
analyst-consensus (adjusted) EPS basis, the same figure the ticker card serves
(``TickerValuation.trailing_pe``): a market price over the quarterly slice's TTM
consensus EPS. A drifting, price-derived snapshot like ``market_cap`` (0012),
written by the universe sync on the same sweep and overwritten each run; nullable,
and null until the quarterly cache holds four reported quarters (or the trailing
year is a loss). The ``stocks`` table already exists (created in 0002), so this
only alters it.

Revision ID: 0016_stock_pe
Revises: 0015_drop_index_constituents
Create Date: 2026-07-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0016_stock_pe"
down_revision: Union[str, None] = "0015_drop_index_constituents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stocks", sa.Column("pe_ratio", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("stocks", "pe_ratio")
