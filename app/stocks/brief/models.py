from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base


class MarketBriefRecord(Base):
    __tablename__ = "stock_market_brief"

    brief_date: Mapped[date] = mapped_column(Date, primary_key=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    tone: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # A list of {"heading", "body"} objects. JSON is portable across SQLite (tests) and
    # Postgres (RDS); the shape is validated on the way in/out by the repository/entity.
    sections: Mapped[list] = mapped_column(JSON, nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)


def get_brief(session: Session, brief_date: date) -> MarketBriefRecord | None:
    return session.execute(
        select(MarketBriefRecord).where(MarketBriefRecord.brief_date == brief_date)
    ).scalar_one_or_none()


def latest_brief(session: Session) -> MarketBriefRecord | None:
    return session.execute(
        select(MarketBriefRecord).order_by(MarketBriefRecord.brief_date.desc()).limit(1)
    ).scalar_one_or_none()


def recent_brief_dates(session: Session, limit: int) -> list[date]:
    return list(
        session.execute(
            select(MarketBriefRecord.brief_date)
            .order_by(MarketBriefRecord.brief_date.desc())
            .limit(limit)
        ).scalars()
    )
