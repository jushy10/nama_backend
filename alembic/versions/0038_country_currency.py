from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0038_country_currency"
down_revision: Union[str, None] = "0037_ev_ebitda"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("stocks", sa.Column("country", sa.String(length=2), nullable=True))
    op.add_column("stocks", sa.Column("currency", sa.String(length=3), nullable=True))
    # The universe stored to date is entirely the US ≥$1B screen — stamp every screened row
    # (a non-null market_cap is the screened gate) with its market so the multi-market filter
    # reads them correctly from the first CA sync onward. Incidental (never-screened) tickers
    # keep NULL until a screen reaches them.
    op.execute(
        "UPDATE stocks SET country = 'US', currency = 'USD' "
        "WHERE market_cap IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("stocks", "currency")
    op.drop_column("stocks", "country")
