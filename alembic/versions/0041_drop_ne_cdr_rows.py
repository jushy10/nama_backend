from typing import Sequence, Union

from alembic import op

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0041_drop_ne_cdr_rows"
down_revision: Union[str, None] = "0040_domicile_country"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # FK children (ON DELETE CASCADE) go with the anchor row. `.NE` is the Cboe Canada suffix;
    # the literal `.` anchors the LIKE to a true suffix (so `STONE` etc. are untouched).
    op.execute(r"DELETE FROM stocks WHERE ticker LIKE '%.NE'")


def downgrade() -> None:
    # The universe screen re-derives every anchor row (minus the now-excluded `.NE`), so there's
    # nothing to restore.
    pass
