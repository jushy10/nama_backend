from datetime import date, datetime

from pydantic import BaseModel


class MarketBriefSectionResponse(BaseModel):
    heading: str
    body: str


class MarketBriefResponse(BaseModel):
    date: date
    generated_at: datetime
    tone: str
    summary: str
    sections: list[MarketBriefSectionResponse]
    model: str | None = None
    disclaimer: str
