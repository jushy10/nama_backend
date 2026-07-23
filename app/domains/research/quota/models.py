from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

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
