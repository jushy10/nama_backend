from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0011_stocks_growth_yoy"
down_revision: Union[str, None] = "0010_stocks_ticker"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("stocks") as batch_op:
        batch_op.add_column(sa.Column("revenue_growth_yoy", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("eps_growth_yoy", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("stocks") as batch_op:
        batch_op.drop_column("eps_growth_yoy")
        batch_op.drop_column("revenue_growth_yoy")
