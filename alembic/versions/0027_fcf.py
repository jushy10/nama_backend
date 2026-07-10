"""free-cash-flow columns

Adds the free-cash-flow feature's persistence, all nullable so they backfill lazily:

- ``stock_annual_earnings`` gains ``fcf_per_share`` / ``ocf_per_share`` — a reported fiscal
  year's free- and operating-cash-flow per share (trading currency), from the cash-flow
  statement over the year's diluted average shares. Persisted per-year so the annual slice's
  merge-preserving sync can carry them forward when Yahoo blocks the (hard-gated) cash-flow
  fetch, exactly as it does for the reported revenue/net-income figures.

- ``stocks`` gains four snapshots written by their syncs: ``fcf_per_share`` / ``ocf_per_share``
  (the newest reported year's per-share cash figures, written by the annual slice beside the
  growth pair — the ticker card prices these against its live quote into P/FCF, FCF yield and
  OCF yield, so the card needs no live cash-flow fetch); ``fcf_growth_yoy`` (trailing YoY growth
  of ``fcf_per_share``, the cash-flow sibling of ``revenue_growth_yoy``, served directly); and
  ``fcf_yield`` (the *materialized* FCF yield the universe sync's valuation pass derives from the
  screen-time price ÷ stored ``fcf_per_share``, the same way it derives ``pe_ratio`` — so the
  stock-search list is sortable by cash cheapness).

Both tables already exist (``stocks`` from 0002, ``stock_annual_earnings`` from 0005), so this
only alters them.

Revision ID: 0027_fcf
Revises: 0026_revenue_segments
Create Date: 2026-07-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0027_fcf"
down_revision: Union[str, None] = "0026_revenue_segments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stock_annual_earnings",
        sa.Column("fcf_per_share", sa.Float(), nullable=True),
    )
    op.add_column(
        "stock_annual_earnings",
        sa.Column("ocf_per_share", sa.Float(), nullable=True),
    )
    op.add_column("stocks", sa.Column("fcf_per_share", sa.Float(), nullable=True))
    op.add_column("stocks", sa.Column("ocf_per_share", sa.Float(), nullable=True))
    op.add_column("stocks", sa.Column("fcf_growth_yoy", sa.Float(), nullable=True))
    op.add_column("stocks", sa.Column("fcf_yield", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("stocks", "fcf_yield")
    op.drop_column("stocks", "fcf_growth_yoy")
    op.drop_column("stocks", "ocf_per_share")
    op.drop_column("stocks", "fcf_per_share")
    op.drop_column("stock_annual_earnings", "ocf_per_share")
    op.drop_column("stock_annual_earnings", "fcf_per_share")
