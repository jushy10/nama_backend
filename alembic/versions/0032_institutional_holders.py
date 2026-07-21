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
