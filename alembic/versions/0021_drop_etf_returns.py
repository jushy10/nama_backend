"""drop the ETF trailing-return ladder columns

Removes ``ytd_return`` / ``three_year_return`` / ``five_year_return`` from ``etfs`` (added by
0020). These annualized trailing returns are no longer persisted: only the detail card's opt-in
``performance`` block surfaces the 3y/5y figures, so the read path now fetches them **live** from
Yahoo (the same ``.info`` blob the enrichment pass already reads) rather than storing a snapshot
that drifts between syncs. The rest of the per-fund profile (fund family, NAV, dividend yield,
description, holdings, sector weightings) stays on the row and its child tables.

``downgrade`` re-adds the three nullable Float columns (empty — a later profile sync would refill
them if the read path went back to storing them).

Revision ID: 0021_drop_etf_returns
Revises: 0020_etf_profile
Create Date: 2026-07-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0021_drop_etf_returns"
down_revision: Union[str, None] = "0020_etf_profile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Batch mode so the DROP works on SQLite too (a table rebuild — used by the offline tests).
    with op.batch_alter_table("etfs") as batch_op:
        batch_op.drop_column("five_year_return")
        batch_op.drop_column("three_year_return")
        batch_op.drop_column("ytd_return")


def downgrade() -> None:
    with op.batch_alter_table("etfs") as batch_op:
        batch_op.add_column(sa.Column("ytd_return", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("three_year_return", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("five_year_return", sa.Float(), nullable=True))
