"""enterprise-value inputs + materialized EV/EBITDA on the anchor

Adds the enterprise-value feature's persistence onto the shared ``stocks`` anchor — the
inputs the ticker card prices live into enterprise value and EV/EBITDA, plus a materialized
EV/EBITDA snapshot the universe search and peer comparison read DB-only (the same "store the
input, price it live" + "materialize a sortable snapshot" split ``pe_ratio`` / ``fcf_yield``
already use). All nullable so they backfill lazily as the syncs reach each stock:

- Enterprise-value *inputs*, landed by the ``sync-fundamentals`` cron off Yahoo ``.info``:
  ``ebitda`` (trailing, absolute), ``total_debt`` and ``cash_and_equivalents`` (absolute) —
  all three in the stock's trading currency (foreign-ADR reporting figures normalized in the
  adapter) — and ``shares_outstanding`` (a count, currency-agnostic). The card computes
  enterprise value = live price × shares + total debt − cash, and EV/EBITDA over ``ebitda``,
  on the live quote — deliberately not a stored snapshot, so the multiple stays fresh like P/E.
- ``ev_to_ebitda`` — the *materialized* EV/EBITDA snapshot the universe sync's valuation pass
  writes (enterprise value from the screen-time market cap + debt − cash, over ``ebitda``),
  overwritten every run like ``pe_ratio``, so the search list and the peer comparison are
  sortable/readable off one DB query. Null until both the fundamentals sync (the inputs) and
  the universe sync (the snapshot) have reached the stock, or on a non-positive EBITDA.

``stocks`` already exists (from 0002), so this only alters it.

Revision ID: 0037_ev_ebitda
Revises: 0036_earnings_session
Create Date: 2026-07-14

"""
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
