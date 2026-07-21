from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0033_stock_performance"
down_revision: Union[str, None] = "0032_institutional_holders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    "perf_one_week",
    "perf_one_month",
    "perf_three_month",
    "perf_six_month",
    "perf_ytd",
    "perf_one_year",
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("stocks", sa.Column(column, sa.Float(), nullable=True))
    op.add_column(
        "stocks",
        sa.Column("performance_synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stocks", "performance_synced_at")
    for column in reversed(_COLUMNS):
        op.drop_column("stocks", column)
