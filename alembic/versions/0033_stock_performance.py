"""stock trailing-performance columns on the anchor

Adds the performance feature's persistence — a stock's trailing price-return over the
standard windows (1W / 1M / 3M / 6M / YTD / 1Y, percent), materialized onto the shared
``stocks`` anchor so the heat map (and, later, the universe search) can read them DB-only
instead of computing them live from a year of daily bars per index on every request (the
"get it from the DB, not the live vendor" move already applied to fundamentals, cash flow,
growth and the trailing P/E).

The trailing windows barely move intra-session — they're derived from split-adjusted daily
closes — but recomputing them for a whole index on the read path was the heat map's heaviest
leg by far (~a year of bars for ~500 names). Materializing them here turns that read into a
single anchor query; the ``sync-stock-performance`` cron keeps them current from Alpaca.

All nullable so they backfill lazily as the cron reaches each stock:

- ``perf_one_week`` / ``perf_one_month`` / ``perf_three_month`` / ``perf_six_month`` /
  ``perf_ytd`` / ``perf_one_year`` — the trailing-window returns (percent), the six fields of
  the shared ``StockPerformance`` value object. Each is ``NULL`` until the sync has enough
  history (a newly listed name), and stays a moving snapshot (overwritten every refresh).
- ``performance_synced_at`` — the freshness stamp the cron orders its stale-first sweep by
  (un-synced rows first, then the oldest), the anchor-column analogue of the earnings slices'
  per-row ``fetched_at`` and the fundamentals slice's ``fundamentals_synced_at``.

``stocks`` already exists (from 0002), so this only alters it.

Revision ID: 0033_stock_performance
Revises: 0032_institutional_holders
Create Date: 2026-07-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0033_stock_performance"
down_revision: Union[str, None] = "0032_institutional_holders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    "perf_one_week",
    "perf_one_month",
    "perf_three_month",
    "perf_six_month",
    "perf_ytd",
    "perf_one_year",
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("stocks", sa.Column(column, sa.Float(), nullable=True))
    op.add_column(
        "stocks",
        sa.Column("performance_synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stocks", "performance_synced_at")
    for column in reversed(_COLUMNS):
        op.drop_column("stocks", column)
