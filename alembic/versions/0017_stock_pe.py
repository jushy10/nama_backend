from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0017_stock_pe"
down_revision: Union[str, None] = "0016_create_etfs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stocks", sa.Column("pe_ratio", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("stocks", "pe_ratio")
