"""HTTP response DTOs for the market-brief endpoints.

Pydantic models kept at the edge, deliberately separate from the ``entities`` — the
serialization shape lives here so the domain stays framework-agnostic. ``date`` is the
JSON name for the entity's ``brief_date`` (mapped in the presenter); ``disclaimer`` is
service-authored at the edge (descriptive, not financial advice), not something the model
writes.
"""

from datetime import date, datetime

from pydantic import BaseModel


class MarketBriefSectionResponse(BaseModel):
    """One section of the brief: a short heading and a plain-language body."""

    heading: str
    body: str


class MarketBriefResponse(BaseModel):
    """A single day's market brief.

    ``date`` is the calendar day it covers; ``generated_at`` is when it was written (UTC);
    ``tone`` is the headline posture (``risk_on`` / ``risk_off`` / ``mixed``); ``summary`` is
    the lede and ``sections`` the ordered body. ``model`` records which model produced it;
    ``disclaimer`` is informational (not financial advice)."""

    date: date
    generated_at: datetime
    tone: str
    summary: str
    sections: list[MarketBriefSectionResponse]
    model: str | None = None
    disclaimer: str
