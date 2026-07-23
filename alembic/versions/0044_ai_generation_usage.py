"""ai_generation_usage — the per-client daily AI-generation quota counter."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0044_ai_generation_usage"
down_revision: Union[str, None] = "0043_recipe_model_required"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_generation_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pool", sa.String(length=16), nullable=False),
        sa.Column("client_key", sa.String(length=64), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "pool", "client_key", "usage_date", name="uq_ai_generation_usage_key"
        ),
    )


def downgrade() -> None:
    op.drop_table("ai_generation_usage")
