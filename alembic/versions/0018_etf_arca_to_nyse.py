"""fold etfs.exchange NYSEARCA into NYSE

Data-only backfill for the ``etfs`` table. The ETF screener used to map Yahoo's
``PCX`` code to ``NYSEARCA``; it now folds NYSE Arca into its parent ``NYSE`` so
``exchange`` stays inside the same four-value vocabulary the stock screen uses
(``NASDAQ``/``NYSE``/``AMEX``/``BATS``). Because ``exchange`` is written *fill-once*
on upsert (a settled value is never clobbered), already-stored ``NYSEARCA`` rows
would keep that value forever, so this rewrites them in place. No schema change.

The downgrade can't faithfully restore which ``NYSE`` rows were Arca (the fold is
lossy), so it's a deliberate no-op.

Revision ID: 0018_etf_arca_nyse
Revises: 0017_stock_pe
Create Date: 2026-07-06

"""
from typing import Sequence, Union

from alembic import op

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0018_etf_arca_nyse"
down_revision: Union[str, None] = "0017_stock_pe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE etfs SET exchange = 'NYSE' WHERE exchange = 'NYSEARCA'")


def downgrade() -> None:
    # The fold is lossy — we can't tell which stored NYSE rows were Arca — so leave them be.
    pass
