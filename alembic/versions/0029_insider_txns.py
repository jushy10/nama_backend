from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0029_insider_txns"
down_revision: Union[str, None] = "0028_scorecard_sections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_insider_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stock_id", sa.Uuid(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=True),
        sa.Column("insider_name", sa.String(length=255), nullable=False),
        sa.Column("officer_title", sa.String(length=255), nullable=True),
        sa.Column("is_director", sa.Boolean(), nullable=False),
        sa.Column("is_officer", sa.Boolean(), nullable=False),
        sa.Column("is_ten_percent_owner", sa.Boolean(), nullable=False),
        sa.Column("security_title", sa.String(length=255), nullable=True),
        sa.Column("transaction_code", sa.String(length=2), nullable=False),
        sa.Column("acquired_disposed", sa.String(length=1), nullable=True),
        sa.Column("shares", sa.Float(), nullable=True),
        sa.Column("price_per_share", sa.Float(), nullable=True),
        sa.Column("shares_owned_following", sa.Float(), nullable=True),
        sa.Column("accession_number", sa.String(length=25), nullable=False),
        sa.Column("line_index", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stock_id",
            "accession_number",
            "line_index",
            name="uq_insider_txn_stock_acc_line",
        ),
    )


def downgrade() -> None:
    op.drop_table("stock_insider_transactions")
