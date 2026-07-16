"""issuer domicile on the anchor (split the US / Canadian screeners by the company's home country)

Adds ``domicile_country`` to the ``stocks`` anchor: the ISO-2 country the *company* is
domiciled in (``US`` / ``CA`` / ``CH`` / ``JP`` / …), read from Yahoo's per-ticker
``.info['country']`` by the universe sync's enrichment pass — distinct from ``country`` (0038),
which is the *listing* market. The two differ exactly where the universe overlaps: a Canadian
company dual-listed in the US (``CNI`` — Canadian National's US line — is listing ``US`` but
domicile ``CA``) and a Canadian Depositary Receipt (``ZCVX.NE`` — a Chevron CDR — is listing
``CA`` but domicile ``US``).

The universe search uses it to split the screeners by *home market*: the US screen shows
US-listed rows except Canadian-domiciled ones (so ``CNI`` drops out, foreign ADRs stay), and the
Canadian screen shows Canadian-listed rows except foreign-domiciled ones (so the CDRs drop out,
Canadian companies stay). It replaces the base-ticker ``has_us_listing`` heuristic (0039), which
couldn't tell a US company's Canadian CDR from a Canadian company's US dual-listing and so
wrongly hid names like ``CP.TO`` / ``CNR.TO``.

Nullable — unset until the enrichment pass reaches the stock (the same per-ticker ``.info`` call
that fills ``sector`` / ``industry``); the search treats an unknown domicile leniently (shown in
its listing market), so the screeners improve as the backfill fills rather than emptying.

``stocks`` already exists (from 0002), so this only alters it.

Revision ID: 0040_domicile_country
Revises: 0039_has_us_listing
Create Date: 2026-07-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0040_domicile_country"
down_revision: Union[str, None] = "0039_has_us_listing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stocks",
        sa.Column("domicile_country", sa.String(length=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stocks", "domicile_country")
