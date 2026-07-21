from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# (RDS) enforces the length even though SQLite ignores it, so a verbose id fails the deploy.
revision: str = "0024_analyst_trends"
down_revision: Union[str, None] = "0023_stock_news"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TARGET_COLUMNS = ("target_mean", "target_high", "target_low", "target_median")


def upgrade() -> None:
    op.rename_table("stock_recommendation_trends", "stock_analyst_trends")
    for name in _TARGET_COLUMNS:
        op.add_column(
            "stock_analyst_trends", sa.Column(name, sa.Float(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("stock_analyst_trends") as batch_op:
        for name in reversed(_TARGET_COLUMNS):
            batch_op.drop_column(name)
    op.rename_table("stock_analyst_trends", "stock_recommendation_trends")
