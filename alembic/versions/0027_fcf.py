from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0027_fcf"
down_revision: Union[str, None] = "0026_revenue_segments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stock_annual_earnings",
        sa.Column("fcf_per_share", sa.Float(), nullable=True),
    )
    op.add_column(
        "stock_annual_earnings",
        sa.Column("ocf_per_share", sa.Float(), nullable=True),
    )
    op.add_column("stocks", sa.Column("fcf_per_share", sa.Float(), nullable=True))
    op.add_column("stocks", sa.Column("ocf_per_share", sa.Float(), nullable=True))
    op.add_column("stocks", sa.Column("fcf_growth_yoy", sa.Float(), nullable=True))
    op.add_column("stocks", sa.Column("fcf_yield", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("stocks", "fcf_yield")
    op.drop_column("stocks", "fcf_growth_yoy")
    op.drop_column("stocks", "ocf_per_share")
    op.drop_column("stocks", "fcf_per_share")
    op.drop_column("stock_annual_earnings", "ocf_per_share")
    op.drop_column("stock_annual_earnings", "fcf_per_share")
