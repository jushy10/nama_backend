import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Keep the revision id <= 32 chars: alembic_version.version_num is VARCHAR(32). Postgres
# enforces it (SQLite doesn't), so a longer id passes local tests but fails the RDS migration.
revision: str = "0042_agent_recipes"
down_revision: Union[str, None] = "0041_drop_ne_cdr_rows"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The DB is the single source of truth for an agent's configuration — the app has no code
# fallback, so this migration both creates the table and seeds the research agent's recipe.
# A prompt/tool/budget change ships as a new migration updating the row.
_RESEARCH_SYSTEM_PROMPT = (
    "You are a stock-research assistant for a US/Canada equity screener. Answer the user's "
    "question using ONLY the tools provided — do not rely on memorized figures, which may be "
    "stale, and never invent a ticker, price, or statistic. Call a tool to get real data, read "
    "its result, and call more tools if you need to before answering.\n"
    "Rules:\n"
    "- Ground every specific number or ticker you state in a tool result from this "
    "conversation. If the tools can't answer, say so plainly rather than guessing.\n"
    "- Be concise and neutral. Explain what the data shows; do not tell the user what to buy, "
    "sell, or hold, and do not give personalized investment advice. If asked for a personal "
    "recommendation (e.g. 'should I put my savings in X'), explain the trade-offs the data "
    "shows and decline to advise.\n"
    "- When you have enough to answer, respond in plain text with no further tool calls."
)

_recipes_table = sa.table(
    "agent_recipes",
    sa.column("id", sa.Uuid),
    sa.column("name", sa.String),
    sa.column("system_prompt", sa.Text),
    sa.column("tool_names", sa.JSON),
    sa.column("max_steps", sa.Integer),
    sa.column("model_id", sa.String),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    op.create_table(
        "agent_recipes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False, unique=True),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("tool_names", sa.JSON(), nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.bulk_insert(
        _recipes_table,
        [
            {
                "id": uuid.uuid4(),
                "name": "research",
                "system_prompt": _RESEARCH_SYSTEM_PROMPT,
                "tool_names": ["search_stocks", "get_market_sentiment"],
                "max_steps": 6,
                "model_id": None,
                "updated_at": datetime.now(timezone.utc),
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("agent_recipes")
