"""index-membership flags on stocks

Adds two boolean membership flags to the ``stocks`` anchor: ``in_sp500`` and
``in_nasdaq100``. Which index a company belongs to is slow-moving reference data (it only
changes on the quarterly index rebalances), so it's folded straight onto the anchor every
feature already references rather than a separate table — the same call 0012 made for the
universe screen facts. Both are ``NOT NULL`` with a ``false`` server default: membership is
known (absent from the source list == not a member), and the default backfills the rows that
already exist. The reconcile cron (``/internal/index-membership/sync``) keeps them current.
The ``stocks`` table already exists (created in 0002), so this only alters it.

Revision ID: 0013_index_flags_on_stocks
Revises: 0012_universe_on_stocks
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0013_index_flags_on_stocks"
down_revision: Union[str, None] = "0012_universe_on_stocks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stocks",
        sa.Column(
            "in_sp500", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "stocks",
        sa.Column(
            "in_nasdaq100", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("stocks", "in_nasdaq100")
    op.drop_column("stocks", "in_sp500")
