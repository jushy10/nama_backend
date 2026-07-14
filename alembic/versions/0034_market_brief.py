"""create stock_market_brief

The daily-market-brief store: one row per calendar date holding an AI-written, plain-language
read of how the whole US market moved that day — a headline ``tone``, a ``summary`` lede, and
an ordered list of narrative ``sections`` (JSON). Unlike the other feature tables it hangs off
**no** ``stocks`` anchor (a brief is about the market, not one company), so it's a standalone
table keyed by ``brief_date`` (the primary key) — a brief is a durable, dated fact, written once
per day by the sync-market-brief cron and served straight from here (never regenerated on a
read). Mirrors app.stocks.brief.models.

Revision ID: 0034_market_brief
Revises: 0033_stock_performance
Create Date: 2026-07-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# (RDS) enforces the length even though SQLite ignores it, so a verbose id fails the deploy.
revision: str = "0034_market_brief"
down_revision: Union[str, None] = "0033_stock_performance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_market_brief",
        sa.Column("brief_date", sa.Date(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tone", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        # A JSON list of {"heading", "body"} objects — portable across SQLite and Postgres.
        sa.Column("sections", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("brief_date"),
    )


def downgrade() -> None:
    op.drop_table("stock_market_brief")
