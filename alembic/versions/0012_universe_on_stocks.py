from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0012_universe_on_stocks"
down_revision: Union[str, None] = "0011_stocks_growth_yoy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stocks", sa.Column("sector", sa.String(length=64), nullable=True))
    op.add_column("stocks", sa.Column("market_cap", sa.Float(), nullable=True))
    op.add_column(
        "stocks",
        sa.Column("screened_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stocks", "screened_at")
    op.drop_column("stocks", "market_cap")
    op.drop_column("stocks", "sector")
