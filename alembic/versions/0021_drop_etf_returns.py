from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0021_drop_etf_returns"
down_revision: Union[str, None] = "0020_etf_profile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Batch mode so the DROP works on SQLite too (a table rebuild — used by the offline tests).
    with op.batch_alter_table("etfs") as batch_op:
        batch_op.drop_column("five_year_return")
        batch_op.drop_column("three_year_return")
        batch_op.drop_column("ytd_return")


def downgrade() -> None:
    with op.batch_alter_table("etfs") as batch_op:
        batch_op.add_column(sa.Column("ytd_return", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("three_year_return", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("five_year_return", sa.Float(), nullable=True))
