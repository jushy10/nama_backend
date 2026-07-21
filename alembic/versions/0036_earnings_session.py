from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0036_earnings_session"
down_revision: Union[str, None] = "0035_congress"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stock_quarterly_earnings",
        sa.Column("report_session", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stock_quarterly_earnings", "report_session")
