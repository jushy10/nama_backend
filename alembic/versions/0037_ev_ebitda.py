from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0037_ev_ebitda"
down_revision: Union[str, None] = "0036_earnings_session"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    "ebitda",
    "total_debt",
    "cash_and_equivalents",
    "shares_outstanding",
    "ev_to_ebitda",
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("stocks", sa.Column(column, sa.Float(), nullable=True))


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("stocks", column)
