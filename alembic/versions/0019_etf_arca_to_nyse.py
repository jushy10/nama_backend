from typing import Sequence, Union

from alembic import op

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0019_etf_arca_nyse"
down_revision: Union[str, None] = "0018_stocks_forward_growth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE etfs SET exchange = 'NYSE' WHERE exchange = 'NYSEARCA'")


def downgrade() -> None:
    # The fold is lossy — we can't tell which stored NYSE rows were Arca — so leave them be.
    pass
