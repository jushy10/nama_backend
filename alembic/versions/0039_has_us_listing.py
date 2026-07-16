"""interlisted flag on the anchor (hide a Canadian listing that duplicates a US one)

Adds ``has_us_listing`` to the ``stocks`` anchor: ``True`` for a Canadian listing that
duplicates a US-listed company — a CDR (``AAPL.NE`` wraps ``AAPL``) or a dual-listed Canadian
company whose ticker matches its US line (``SHOP.TO`` ↔ ``SHOP``). The universe search hides
these by default (a client sees the US listing, not the Canadian duplicate), so a Canadian
search returns only the companies that *don't* already trade in the US.

It's a screen-derived fact the universe sync recomputes every run: the CA pass matches each
Canadian listing's base ticker (suffix stripped) against the US names already on the anchor.
``NOT NULL`` with a ``False`` default — every US listing and every Canadian-only listing is
``False``; only the interlisted duplicates flip to ``True`` (and the flag is overwritten each
run, so a listing that gains/loses a US sibling is reclassified). The default covers the
backfill: existing rows read ``False`` until the next CA sync recomputes them.

``stocks`` already exists (from 0002), so this only alters it.

Revision ID: 0039_has_us_listing
Revises: 0038_country_currency
Create Date: 2026-07-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0039_has_us_listing"
down_revision: Union[str, None] = "0038_country_currency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stocks",
        sa.Column(
            "has_us_listing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("stocks", "has_us_listing")
