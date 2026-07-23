from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, Integer, String, UniqueConstraint, Uuid, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base


class GenerationUsageRecord(Base):
    """AI generations spent per (pool, client, day). Not a `stocks` child — keyed by
    caller, not ticker; past-day rows are safe to prune."""

    __tablename__ = "ai_generation_usage"
    __table_args__ = (
        UniqueConstraint(
            "pool", "client_key", "usage_date", name="uq_ai_generation_usage_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # "analysis" (the per-symbol AI reads) or "research" (the agent).
    pool: Mapped[str] = mapped_column(String(16), nullable=False)
    # Client IP today; swap-able for a user/device id later.
    client_key: Mapped[str] = mapped_column(String(64), nullable=False)
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)


# Query helpers beside the model (the anchor's get_or_create_stock pattern) — the
# db_repository composes these and owns the transaction (commit/rollback stays there).


def increment_usage_if_below(
    session: Session, pool: str, client_key: str, day: date, limit: int
) -> bool:
    """Atomic conditional increment: True when a row was bumped (was under `limit`)."""
    result = session.execute(
        update(GenerationUsageRecord)
        .where(
            GenerationUsageRecord.pool == pool,
            GenerationUsageRecord.client_key == client_key,
            GenerationUsageRecord.usage_date == day,
            GenerationUsageRecord.count < limit,
        )
        .values(count=GenerationUsageRecord.count + 1)
    )
    return bool(result.rowcount)


def usage_exists(session: Session, pool: str, client_key: str, day: date) -> bool:
    return (
        session.execute(
            select(GenerationUsageRecord.id).where(
                GenerationUsageRecord.pool == pool,
                GenerationUsageRecord.client_key == client_key,
                GenerationUsageRecord.usage_date == day,
            )
        ).first()
        is not None
    )


def insert_usage(session: Session, pool: str, client_key: str, day: date) -> None:
    """Stage the day's first-use row (count=1); the caller commits."""
    session.add(
        GenerationUsageRecord(pool=pool, client_key=client_key, usage_date=day, count=1)
    )
