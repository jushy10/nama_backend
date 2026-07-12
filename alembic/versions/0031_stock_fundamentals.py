"""stock fundamentals columns on the anchor

Adds the fundamentals feature's persistence — the trailing valuation/profitability/health
figures the app currently reads live from Finnhub, materialized onto the shared ``stocks``
anchor so the ticker card and AI analysis can read them DB-only (the same "get it from the
DB, not the live vendor" move already applied to cash flow, growth and the trailing P/E).
All nullable so they backfill lazily as the ``sync-fundamentals`` cron reaches each stock:

- Served directly (currency-agnostic ratios, on a ~quarterly clock): ``gross_margin`` /
  ``operating_margin`` / ``net_margin`` / ``return_on_equity`` (percent), ``current_ratio``,
  ``debt_to_equity`` (a ratio), and ``beta``.
- Per-share *inputs* the readers price against the live quote (the "store the input, price it
  live" split ``fcf_per_share`` / ``ttm_eps`` already use): ``book_value_per_share`` → P/B,
  ``sales_per_share`` → P/S, ``dividend_per_share`` → dividend yield. All in the stock's
  trading currency (foreign-ADR reporting figures normalized in the adapter).
- ``fundamentals_synced_at`` — the freshness stamp the cron orders its stale-first sweep by
  (un-synced rows first, then the oldest), the anchor-column analogue of the earnings slices'
  per-row ``fetched_at``.

``stocks`` already exists (from 0002), so this only alters it.

Revision ID: 0031_stock_fundamentals
Revises: 0030_ai_analysis_kinds
Create Date: 2026-07-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0031_stock_fundamentals"
down_revision: Union[str, None] = "0030_ai_analysis_kinds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    "gross_margin",
    "operating_margin",
    "net_margin",
    "return_on_equity",
    "current_ratio",
    "debt_to_equity",
    "beta",
    "book_value_per_share",
    "sales_per_share",
    "dividend_per_share",
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("stocks", sa.Column(column, sa.Float(), nullable=True))
    op.add_column(
        "stocks",
        sa.Column("fundamentals_synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stocks", "fundamentals_synced_at")
    for column in reversed(_COLUMNS):
        op.drop_column("stocks", column)
