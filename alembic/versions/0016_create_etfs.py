from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0016_create_etfs"
down_revision: Union[str, None] = "0015_drop_index_constituents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "etfs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("exchange", sa.String(length=32), nullable=True),
        sa.Column("net_assets", sa.Float(), nullable=True),
        sa.Column("expense_ratio", sa.Float(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("screened_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticker", name="uq_etfs_ticker"),
    )


def downgrade() -> None:
    op.drop_table("etfs")
