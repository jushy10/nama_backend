from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0043_recipe_model_required"
down_revision: Union[str, None] = "0042_agent_recipes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every recipe now names its model explicitly — no code/env fallback chain.
_DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def upgrade() -> None:
    op.execute(
        sa.text("UPDATE agent_recipes SET model_id = :model_id WHERE model_id IS NULL").bindparams(
            model_id=_DEFAULT_MODEL_ID
        )
    )
    with op.batch_alter_table("agent_recipes") as batch:
        batch.alter_column("model_id", existing_type=sa.String(length=128), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("agent_recipes") as batch:
        batch.alter_column("model_id", existing_type=sa.String(length=128), nullable=True)
