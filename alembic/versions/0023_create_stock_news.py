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
