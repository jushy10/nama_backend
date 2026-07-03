"""add exchange to stocks

Adds the listing exchange (e.g. "NASDAQ") to the shared ``stocks`` anchor. Like
``name``, it's anchor-level data any feature may fill — the ticker card fills it
lazily from the Alpaca snapshot on first view and serves it from the DB after,
because a stock's exchange effectively never changes, so one fetch per symbol is
enough. Nullable — a lazily-created row starts without it. Batch mode so the
ADD/DROP works on SQLite too.

Revision ID: 0009_stocks_exchange
Revises: 0008_annual_eps_consensus
Create Date: 2026-07-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0009_stocks_exchange"
down_revision: Union[str, None] = "0008_annual_eps_consensus"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("stocks") as batch_op:
        batch_op.add_column(sa.Column("exchange", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("stocks") as batch_op:
        batch_op.drop_column("exchange")
