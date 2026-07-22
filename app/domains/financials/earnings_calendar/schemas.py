from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class EarningsCalendarItemResponse(BaseModel):
    ticker: str
    name: str | None = None
    sector: str | None = None
    when: date
    session: str
    market_cap: float | None = None


class EarningsCalendarDayResponse(BaseModel):
    date: date
    count: int
    items: list[EarningsCalendarItemResponse]


class EarningsCalendarResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: date = Field(alias="from")
    to: date
    count: int
    days: list[EarningsCalendarDayResponse]
    disclaimer: str
