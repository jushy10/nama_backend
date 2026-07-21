from typing import Sequence, Union

from alembic import op

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0010_stocks_ticker"
down_revision: Union[str, None] = "0009_stocks_exchange"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("stocks") as batch_op:
        batch_op.alter_column("symbol", new_column_name="ticker")


def downgrade() -> None:
    with op.batch_alter_table("stocks") as batch_op:
        batch_op.alter_column("ticker", new_column_name="symbol")
