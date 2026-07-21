from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0030_ai_analysis_kinds"
down_revision: Union[str, None] = "0029_insider_txns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Batch mode so SQLite recreates the table (it can't ALTER a column's nullability
    # in place); Postgres just emits the ALTERs. Both column adds and nullability
    # relaxes ride the one recreate.
    with op.batch_alter_table("investment_analysis_cache") as batch_op:
        batch_op.add_column(sa.Column("verdict", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("findings", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("details", sa.JSON(), nullable=True))
        batch_op.alter_column(
            "recommendation", existing_type=sa.String(length=16), nullable=True
        )
        batch_op.alter_column(
            "confidence", existing_type=sa.String(length=16), nullable=True
        )
        batch_op.alter_column("strengths", existing_type=sa.JSON(), nullable=True)
        batch_op.alter_column("risks", existing_type=sa.JSON(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("investment_analysis_cache") as batch_op:
        batch_op.alter_column("risks", existing_type=sa.JSON(), nullable=False)
        batch_op.alter_column("strengths", existing_type=sa.JSON(), nullable=False)
        batch_op.alter_column(
            "confidence", existing_type=sa.String(length=16), nullable=False
        )
        batch_op.alter_column(
            "recommendation", existing_type=sa.String(length=16), nullable=False
        )
        batch_op.drop_column("details")
        batch_op.drop_column("findings")
        batch_op.drop_column("verdict")
