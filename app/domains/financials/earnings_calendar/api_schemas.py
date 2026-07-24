from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.domains.financials.earnings_calendar.entities import (
    EarningsCalendar,
    EarningsCalendarDay,
    EarningsCalendarItem,
)

# Informational, attached at the edge — a schedule of who reports when, not financial advice.
_DISCLAIMER = (
    "Scheduled earnings dates are estimates that can change and are for general information "
    "only — not financial advice."
)


class EarningsCalendarItemResponse(BaseModel):
    ticker: str
    name: str | None = None
    sector: str | None = None
    when: date
    session: str
    market_cap: float | None = None

    @classmethod
    def from_item(cls, item: EarningsCalendarItem) -> "EarningsCalendarItemResponse":
        return cls(
            ticker=item.ticker,
            name=item.name,
            sector=item.sector,
            when=item.report_date,
            session=item.session.value,
            market_cap=item.market_cap,
        )


class EarningsCalendarDayResponse(BaseModel):
    date: date
    count: int
    items: list[EarningsCalendarItemResponse]

    @classmethod
    def from_day(cls, day: EarningsCalendarDay) -> "EarningsCalendarDayResponse":
        return cls(
            date=day.date,
            count=len(day.items),
            items=[EarningsCalendarItemResponse.from_item(item) for item in day.items],
        )


class EarningsCalendarResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: date = Field(alias="from")
    to: date
    count: int
    days: list[EarningsCalendarDayResponse]
    disclaimer: str

    @classmethod
    def from_calendar(cls, calendar: EarningsCalendar) -> "EarningsCalendarResponse":
        return cls(
            from_=calendar.from_date,
            to=calendar.to_date,
            count=calendar.count,
            days=[EarningsCalendarDayResponse.from_day(day) for day in calendar.days],
            disclaimer=_DISCLAIMER,
        )
