"""country + trading currency on the anchor (multi-market universe)

Adds the two screen facts that let the universe hold more than one market — the US screen
plus the Canadian (TSX/TSXV) screen — without conflating them:

- ``country`` — the listing country as an ISO-2 code (``US`` / ``CA``), stamped by the
  universe sync per screen pass. It's what a client filters the universe by market on and
  what routes a stock's price feed (US → Alpaca, CA → Yahoo).
- ``currency`` — the trading currency the row's ``market_cap`` (and every price-derived
  figure) is quoted in, as an ISO-3 code (``USD`` / ``CAD``). It exists because the
  ≥$1B floor is applied in each market's **native** currency (Yahoo's screener reports a
  quote in its own currency), so a CAD row's ``market_cap`` is whole CAD and must carry its
  unit — a mixed-currency ``market_cap`` sort is nominal, and the FE labels/converts off this.

Both are screen facts like ``sector`` / ``market_cap`` (filled by the sync, nullable for an
incidentally-known ticker that's never been screened), so they're nullable. The existing
universe is entirely the US screen, so backfill every already-screened row (``market_cap``
not null) to ``US`` / ``USD``; incidental non-screened tickers stay null until a screen
reaches them.

``stocks`` already exists (from 0002), so this only alters it.

Revision ID: 0038_country_currency
Revises: 0037_ev_ebitda
Create Date: 2026-07-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0038_country_currency"
down_revision: Union[str, None] = "0037_ev_ebitda"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stocks", sa.Column("country", sa.String(length=2), nullable=True))
    op.add_column("stocks", sa.Column("currency", sa.String(length=3), nullable=True))
    # The universe stored to date is entirely the US ≥$1B screen — stamp every screened row
    # (a non-null market_cap is the screened gate) with its market so the multi-market filter
    # reads them correctly from the first CA sync onward. Incidental (never-screened) tickers
    # keep NULL until a screen reaches them.
    op.execute(
        "UPDATE stocks SET country = 'US', currency = 'USD' "
        "WHERE market_cap IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("stocks", "currency")
    op.drop_column("stocks", "country")
