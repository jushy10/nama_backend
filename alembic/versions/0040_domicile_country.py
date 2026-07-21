from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0040_domicile_country"
down_revision: Union[str, None] = "0039_has_us_listing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stocks",
        sa.Column("domicile_country", sa.String(length=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stocks", "domicile_country")
