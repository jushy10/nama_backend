"""analysis cache

Creates ``investment_analysis_cache`` — the read-through result cache behind the AI
analysis endpoints (``GET /stocks/{symbol}/analysis`` and
``GET /stocks/etf/{ticker}/analysis``). One row per ``(kind, symbol)`` holds the most
recent generated read so a repeat view — and a burst of viewers — skips the expensive
data gather + model call. It is a cache, not a source of record: rows are regenerated
once they age past the endpoint's TTL, and it deliberately does not hang off the
``stocks`` anchor (an analysis is served for any valid ticker; forcing an anchor row
would leak arbitrary tickers into the screened universe).

Revision ID: 0022_analysis_cache
Revises: 0021_drop_etf_returns
Create Date: 2026-07-08 15:22:19.482834

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# (RDS) enforces the length even though SQLite ignores it, so a verbose id fails the deploy.
revision: str = "0022_analysis_cache"
down_revision: Union[str, None] = "0021_drop_etf_returns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "investment_analysis_cache",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("recommendation", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("thesis", sa.Text(), nullable=False),
        sa.Column("strengths", sa.JSON(), nullable=False),
        sa.Column("risks", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "kind", "symbol", name="uq_investment_analysis_cache_kind_symbol"
        ),
    )


def downgrade() -> None:
    op.drop_table("investment_analysis_cache")
