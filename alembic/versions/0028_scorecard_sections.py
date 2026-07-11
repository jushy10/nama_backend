"""scorecard sections column

Adds ``sections`` (nullable JSON) to ``investment_analysis_cache`` — the stock
analysis endpoint moved from the flat ``strengths``/``risks`` bullet lists to a
sectioned ``StockScorecard`` (business quality / valuation / earnings / analyst
view), stored here for ``kind="stock"`` rows. Nullable: the ETF rows
(``kind="etf"``) keep using ``strengths``/``risks`` and leave this null, and — being
a cache — any pre-existing stock rows simply miss and regenerate into the new shape.

The table already exists (``investment_analysis_cache`` from 0022), so this only
alters it.

Revision ID: 0028_scorecard_sections
Revises: 0027_fcf
Create Date: 2026-07-11

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0028_scorecard_sections"
down_revision: Union[str, None] = "0027_fcf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "investment_analysis_cache",
        sa.Column("sections", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("investment_analysis_cache", "sections")
