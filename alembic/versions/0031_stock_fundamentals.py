from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0031_stock_fundamentals"
down_revision: Union[str, None] = "0030_ai_analysis_kinds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    "gross_margin",
    "operating_margin",
    "net_margin",
    "return_on_equity",
    "current_ratio",
    "debt_to_equity",
    "beta",
    "book_value_per_share",
    "sales_per_share",
    "dividend_per_share",
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("stocks", sa.Column(column, sa.Float(), nullable=True))
    op.add_column(
        "stocks",
        sa.Column("fundamentals_synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stocks", "fundamentals_synced_at")
    for column in reversed(_COLUMNS):
        op.drop_column("stocks", column)
