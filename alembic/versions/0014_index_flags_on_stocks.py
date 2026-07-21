from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0014_index_flags_on_stocks"
down_revision: Union[str, None] = "0013_stocks_industry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stocks",
        sa.Column(
            "in_sp500", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "stocks",
        sa.Column(
            "in_nasdaq100", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("stocks", "in_nasdaq100")
    op.drop_column("stocks", "in_sp500")
