"""merge the concurrent 0028 heads (insider transactions + scorecard sections)

Two feature slices landed a ``0028`` migration off ``0027_fcf`` at the same time —
``0028_insider_txns`` and ``0028_scorecard_sections`` — which branched the revision history
into **two heads**. ``alembic upgrade head`` refuses a multiple-head target, so migrations
(and any deploy that runs them) fail until the heads are rejoined.

This is a no-op **merge migration**: it makes no schema change, it only unifies the two heads
back into a single linear head so ``upgrade head`` resolves again. Standard fix for the
concurrent-development collision; nothing to undo on downgrade.

Revision ID: 0029_merge_heads
Revises: 0028_insider_txns, 0028_scorecard_sections
Create Date: 2026-07-11

"""
from typing import Sequence, Union

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0029_merge_heads"
down_revision: Union[str, Sequence[str], None] = (
    "0028_insider_txns",
    "0028_scorecard_sections",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No schema change — this migration only merges the two heads."""
    pass


def downgrade() -> None:
    """No schema change to reverse."""
    pass
