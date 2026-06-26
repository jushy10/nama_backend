"""create index_constituents

The stock screener's universe table — S&P 500 / Nasdaq-100 membership + GICS
sector per symbol. Mirrors app.stocks.constituents.ConstituentRecord. Populated
out of band by scripts/sync_constituents.py (FMP -> DB), so it starts empty.

Revision ID: 0001_index_constituents
Revises:
Create Date: 2026-06-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_index_constituents"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "index_constituents",
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("sector", sa.String(length=64), nullable=True),
        sa.Column("in_sp500", sa.Boolean(), nullable=False),
        sa.Column("in_nasdaq100", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("symbol"),
    )


def downgrade() -> None:
    op.drop_table("index_constituents")
