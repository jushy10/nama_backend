from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base


class AgentRecipeRecord(Base):
    """One agent's stored recipe — seeded and updated by migrations, never by the app."""

    __tablename__ = "agent_recipes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    tool_names: Mapped[list] = mapped_column(JSON, nullable=False)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def recipe_by_name(session: Session, name: str) -> AgentRecipeRecord | None:
    return session.execute(
        select(AgentRecipeRecord).where(AgentRecipeRecord.name == name)
    ).scalar_one_or_none()
