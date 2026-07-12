"""create institutional-ownership tables

Two children of the ``stocks`` anchor for the institutional-ownership ("big money buys and sells")
slice:

- ``stock_institutional_holders`` — a time series: the top 13F holders (institutions + mutual funds)
  of a stock as of each reported quarter, one row per holder per quarter, unique on
  ``(stock_id, holder_type, holder, date_reported)``. ``pct_change`` (percent) is the
  quarter-over-quarter change in the holder's position — the buy/sell signal. Like the news table a
  refresh *merges* (replaces the snapshots it re-served, keeps earlier reported quarters), so the
  store accumulates a longer history than the source serves at once — pruned to the newest N per
  stock in the repository so it stays bounded.
- ``stock_ownership_summary`` — one row per stock (unique on ``stock_id``): the current "institutions
  own X% of the float" breakdown, overwritten each refresh (Yahoo publishes only a current snapshot).

Both filled lazily on a miss and refreshed by the institutional-ownership cron (yfinance -> DB), so
they start empty. The ``stocks`` anchor already exists (created in 0002), so this migration only adds
the two child tables. Mirrors app.stocks.institutional_ownership.models.

Revision ID: 0032_institutional_holders
Revises: 0031_stock_fundamentals
Create Date: 2026-07-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres (RDS)
# enforces the length even though SQLite ignores it, so a verbose id fails the deploy.
revision: str = "0032_institutional_holders"
down_revision: Union[str, None] = "0031_stock_fundamentals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_institutional_holders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stock_id", sa.Uuid(), nullable=False),
        sa.Column("holder", sa.String(length=255), nullable=False),
        sa.Column("holder_type", sa.String(length=16), nullable=False),
        sa.Column("date_reported", sa.Date(), nullable=False),
        sa.Column("shares", sa.Float(), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("pct_held", sa.Float(), nullable=True),
        sa.Column("pct_change", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stock_id",
            "holder_type",
            "holder",
            "date_reported",
            name="uq_inst_holder_stock_type_holder_date",
        ),
    )
    op.create_table(
        "stock_ownership_summary",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stock_id", sa.Uuid(), nullable=False),
        sa.Column("institutions_pct_held", sa.Float(), nullable=True),
        sa.Column("insiders_pct_held", sa.Float(), nullable=True),
        sa.Column("institutions_float_pct_held", sa.Float(), nullable=True),
        sa.Column("institutions_count", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stock_id", name="uq_ownership_summary_stock"),
    )


def downgrade() -> None:
    op.drop_table("stock_ownership_summary")
    op.drop_table("stock_institutional_holders")
