from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from app.stocks.ai.brief import models
from app.stocks.ai.brief.entities import (
    BriefTone,
    MarketBrief,
    MarketBriefSection,
)
from app.stocks.ai.brief.models import MarketBriefRecord
from app.stocks.ai.brief.interfaces import MarketBriefRepository


def _to_entity(row: MarketBriefRecord) -> MarketBrief:
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
