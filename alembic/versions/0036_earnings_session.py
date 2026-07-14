"""announcement session (before-open / after-close) on quarterly earnings

Adds ``report_session`` to ``stock_quarterly_earnings`` — the market-timing of each
announcement relative to the US session: before market open, after market close, intraday,
or unknown (the ``EarningsSession`` enum's values ``bmo`` / ``amc`` / ``during`` / ``unknown``).

The signal was already present on Yahoo's ``earnings_dates`` index (the announcement's
time-of-day, in Eastern time) but the adapter was collapsing the timestamp to a bare date
before storage, so before-open vs after-close was lost. The adapter now classifies it from
the time-of-day; this column persists it so the read endpoints — the quarterly-earnings
timeline and, primarily, the market-wide earnings calendar (``GET /market/earnings-calendar``,
which projects these rows) — can tell a client whether a company reports BMO or AMC.

Nullable, no backfill: rows written before this column exists read back as ``NULL``, which the
repository maps to ``UNKNOWN``. The ``sync-quarterly-earnings`` cron populates it as it rewrites
each stock's window (delete-then-insert), so existing rows fill in on the next sweep.

Revision ID: 0036_earnings_session
Revises: 0035_congress
Create Date: 2026-07-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0036_earnings_session"
down_revision: Union[str, None] = "0035_congress"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stock_quarterly_earnings",
        sa.Column("report_session", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stock_quarterly_earnings", "report_session")
