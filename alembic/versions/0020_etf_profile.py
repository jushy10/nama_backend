from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0020_etf_profile"
down_revision: Union[str, None] = "0019_etf_arca_nyse"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Batch mode so the ADD works on SQLite too (used by the offline tests).
    with op.batch_alter_table("etfs") as batch_op:
        batch_op.add_column(sa.Column("fund_family", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("dividend_yield", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("nav", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("ytd_return", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("three_year_return", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("five_year_return", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column("profile_fetched_at", sa.DateTime(timezone=True), nullable=True)
        )

    op.create_table(
        "etf_sector_weightings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("etf_id", sa.Uuid(), nullable=False),
        sa.Column("sector", sa.String(length=64), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["etf_id"], ["etfs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "etf_id", "sector", name="uq_etf_sector_weightings_etf_sector"
        ),
    )

    op.create_table(
        "etf_top_holdings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("etf_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["etf_id"], ["etfs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "etf_id", "position", name="uq_etf_top_holdings_etf_position"
        ),
    )


def downgrade() -> None:
    op.drop_table("etf_top_holdings")
    op.drop_table("etf_sector_weightings")
    with op.batch_alter_table("etfs") as batch_op:
        batch_op.drop_column("profile_fetched_at")
        batch_op.drop_column("five_year_return")
        batch_op.drop_column("three_year_return")
        batch_op.drop_column("ytd_return")
        batch_op.drop_column("nav")
        batch_op.drop_column("description")
        batch_op.drop_column("dividend_yield")
        batch_op.drop_column("fund_family")
