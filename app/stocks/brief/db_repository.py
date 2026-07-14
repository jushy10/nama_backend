"""Interface Adapter: the SQLAlchemy-backed MarketBriefRepository.

Implements the ``repository.py`` port against the ``stock_market_brief`` table. Its job is
the mapping the use cases must not see: it converts the ``MarketBrief`` entity to and from
the ORM row (the ``sections`` tuple ⇄ the JSON list of ``{"heading", "body"}`` objects) and
delegates every query to ``models.py``. ``upsert`` writes one row per date — replacing the
day's row if it already exists — and commits its own write, so a successful generation is
durable independent of the request/task.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.stocks.brief import models
from app.stocks.brief.entities import (
    BriefTone,
    MarketBrief,
    MarketBriefSection,
)
from app.stocks.brief.models import MarketBriefRecord
from app.stocks.brief.repository import MarketBriefRepository


def _to_entity(row: MarketBriefRecord) -> MarketBrief:
    """Row -> entity. Defensive on the JSON ``sections`` (skip any malformed element) and the
    ``tone`` slug (fall back to ``mixed`` if an unknown value ever reached the row), so a
    read never raises on stored data."""
    sections = tuple(
        MarketBriefSection(heading=str(s["heading"]), body=str(s["body"]))
        for s in (row.sections or [])
        if isinstance(s, dict) and s.get("heading") and s.get("body")
    )
    try:
        tone = BriefTone(row.tone)
    except ValueError:
        tone = BriefTone.MIXED
    return MarketBrief(
        brief_date=row.brief_date,
        generated_at=row.generated_at,
        tone=tone,
        summary=row.summary,
        sections=sections,
        model=row.model or "",
    )


class SqlMarketBriefRepository(MarketBriefRepository):
    """Reads and writes the daily briefs through a request/task-scoped session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, brief_date: date) -> MarketBrief | None:
        row = models.get_brief(self._session, brief_date)
        return _to_entity(row) if row is not None else None

    def latest(self) -> MarketBrief | None:
        row = models.latest_brief(self._session)
        return _to_entity(row) if row is not None else None

    def upsert(self, brief: MarketBrief) -> None:
        sections = [
            {"heading": s.heading, "body": s.body} for s in brief.sections
        ]
        row = models.get_brief(self._session, brief.brief_date)
        if row is None:
            row = MarketBriefRecord(brief_date=brief.brief_date)
            self._session.add(row)
        # Overwrite in place: a re-run for a date replaces that day's brief rather than
        # accumulating rows (the date is the primary key).
        row.generated_at = brief.generated_at
        row.tone = brief.tone.value
        row.summary = brief.summary
        row.sections = sections
        row.model = brief.model or None
        self._session.commit()
