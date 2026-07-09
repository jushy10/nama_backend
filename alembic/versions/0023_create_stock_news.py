"""create stock_news

The news cache: a time-series child of the ``stocks`` anchor holding a stock's recent
news articles — many rows per stock, one per article, unique on ``(stock_id,
article_id)`` (the source's stable article id). Mirrors app.stocks.news.models. Filled
lazily on a miss and refreshed by the news cron endpoint (yfinance -> DB), so it starts
empty. Like the recommendations table a refresh *merges* (a published article is a frozen
fact), so the store accumulates a longer feed than the ~10 items Yahoo serves at once —
but pruned to the newest N per stock (in the repository) so the higher-volume news history
stays bounded. The ``stocks`` anchor already exists (created in 0002), so this migration
only adds the child table.

Revision ID: 0023_stock_news
Revises: 0022_analysis_cache
Create Date: 2026-07-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# (RDS) enforces the length even though SQLite ignores it, so a verbose id fails the deploy.
revision: str = "0023_stock_news"
down_revision: Union[str, None] = "0022_analysis_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_news",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stock_id", sa.Uuid(), nullable=False),
        sa.Column("article_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("publisher", sa.String(length=128), nullable=True),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(length=16), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stock_id",
            "article_id",
            name="uq_stock_news_stock_article",
        ),
    )


def downgrade() -> None:
    op.drop_table("stock_news")
