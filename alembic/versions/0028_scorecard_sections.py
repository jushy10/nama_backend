from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0028_scorecard_sections"
down_revision: Union[str, None] = "0027_fcf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "investment_analysis_cache",
        sa.Column("sections", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("investment_analysis_cache", "sections")
