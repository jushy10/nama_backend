"""congressional stock trades cache table

Adds ``stock_congress_trades`` — the persistence for the Congressional-trades slice: the buys and
sells US Representatives and Senators disclose under the STOCK Act, one row per disclosed trade.

A time series keyed unique on ``(stock_id, member, transaction_date, amount_range, chamber)`` — the
identity of a Congressional disclosure. The slice's DB-only read serves stored rows; a weekly cron
(``SyncCongressTrades``) fetches the whole market-wide feed once and distributes it, insert-only (a
filed disclosure is a frozen fact) with the feed pruned to the newest N trades per stock. Hangs off
the ``stocks`` anchor (from 0002) with an ``ON DELETE CASCADE`` foreign key.

Revision ID: 0035_congress
Revises: 0034_market_brief
Create Date: 2026-07-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres enforces
# it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
# Chained onto 0034_market_brief (not 0033) so the two concurrently-authored 0034 migrations
# form one linear history rather than two alembic heads.
revision: str = "0035_congress"
down_revision: Union[str, None] = "0034_market_brief"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_congress_trades",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stock_id", sa.Uuid(), nullable=False),
        sa.Column("member", sa.String(length=160), nullable=False),
        sa.Column("chamber", sa.String(length=16), nullable=False),
        sa.Column("party", sa.String(length=16), nullable=True),
        sa.Column("tx_type", sa.String(length=16), nullable=False),
        sa.Column("amount_range", sa.String(length=64), nullable=True),
        sa.Column("transaction_date", sa.Date(), nullable=True),
        sa.Column("disclosure_date", sa.Date(), nullable=True),
        sa.Column("owner", sa.String(length=32), nullable=True),
        sa.Column("source_url", sa.String(length=512), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stock_id",
            "member",
            "transaction_date",
            "amount_range",
            "chamber",
            name="uq_congress_stock_member_date_amount_chamber",
        ),
    )


def downgrade() -> None:
    op.drop_table("stock_congress_trades")
