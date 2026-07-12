"""ai analysis cache — five more kinds

Extends ``investment_analysis_cache`` (migration 0022) so the earnings, ratings,
fundamentals, sector and market AI reads share the same read-through result cache the
stock scorecard and ETF analysis already use — instead of re-calling the model on every
server-side miss and racking up tokens.

Adds three nullable columns the newer kinds share:

- ``verdict`` — their headline enum (earnings ``trend``, ratings/fundamentals
  ``verdict``, sector/market ``tone``),
- ``findings`` — the flat takeaway list (earnings ``highlights``, ratings/fundamentals
  ``findings``),
- ``details`` — the market-wide nested structure (sector ``{leaders, laggards}``,
  market ``{periods}``).

And relaxes the stock/ETF-only columns ``recommendation`` / ``confidence`` /
``strengths`` / ``risks`` to nullable, since the newer kinds don't all carry them
(ratings/fundamentals reuse ``confidence``; everyone else leaves them null).
``thesis`` / ``symbol`` / ``model`` / ``generated_at`` stay NOT NULL. The market-wide
kinds key on a sentinel ``symbol``. Existing stock/ETF rows are untouched.

Revision ID: 0030_ai_analysis_kinds
Revises: 0029_insider_txns
Create Date: 2026-07-11

"""
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
