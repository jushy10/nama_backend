"""Database model + queries for the daily market brief.

The persistence primitives for the slice: the SQLAlchemy model for the ``stock_market_brief``
table this feature owns, plus simple, entity-free query functions over it. Unlike the other
slices' tables this one hangs off **no** ``stocks`` anchor — a brief is about the whole
market, not one company — so it's a standalone table keyed by calendar date (like the
``investment_analysis_cache`` for the market-wide AI reads). One row per date.

The concrete repository (``db_repository.py``) is the only caller; it maps these rows to and
from the ``MarketBrief`` entity. Nothing here knows the domain entity — this layer deals only
in rows and columns.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base


class MarketBriefRecord(Base):
    """One day's stored market brief.

    ``brief_date`` is the primary key — a brief is a fact about a single calendar day, so a
    day has exactly one brief (a re-run overwrites it). ``tone`` is the headline-posture slug
    (``risk_on`` / ``risk_off`` / ``mixed``); ``summary`` the plain-language lede; ``sections``
    the ordered body as a JSON list of ``{"heading": ..., "body": ...}`` objects (an open list,
    so the model shapes the day's story without a fixed column per section); ``model`` records
    which model produced it.
    """

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
    """The stored brief for one date, or ``None`` when no brief was written that day."""
    return session.execute(
        select(MarketBriefRecord).where(MarketBriefRecord.brief_date == brief_date)
    ).scalar_one_or_none()


def latest_brief(session: Session) -> MarketBriefRecord | None:
    """The most recent brief by date, or ``None`` when the store is empty."""
    return session.execute(
        select(MarketBriefRecord).order_by(MarketBriefRecord.brief_date.desc()).limit(1)
    ).scalar_one_or_none()


def recent_brief_dates(session: Session, limit: int) -> list[date]:
    """The most recent ``limit`` brief dates, newest first — the set of dated brief pages
    that exist, for the SEO sitemap. Empty when nothing is stored yet."""
    return list(
        session.execute(
            select(MarketBriefRecord.brief_date)
            .order_by(MarketBriefRecord.brief_date.desc())
            .limit(limit)
        ).scalars()
    )
