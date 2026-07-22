from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# The market-timing enum is owned by the quarterly slice (the source of these rows); the
# calendar is a projection of that slice, so it consumes the same domain type rather than
# re-defining a parallel one — the same direction the calendar's db_repository already
# depends on the quarterly models.
from app.domains.financials.earnings.quarterly.entities import EarningsSession


@dataclass(frozen=True)
class EarningsCalendarItem:
    ticker: str
    name: str | None
    sector: str | None
    report_date: date
    session: EarningsSession = EarningsSession.UNKNOWN
    market_cap: float | None = None


@dataclass(frozen=True)
class EarningsCalendarDay:
    date: date
    items: tuple[EarningsCalendarItem, ...]


@dataclass(frozen=True)
class EarningsCalendar:
    from_date: date
    to_date: date
    days: tuple[EarningsCalendarDay, ...]

    @property
    def count(self) -> int:
        return sum(len(day.items) for day in self.days)

    @classmethod
    def build(
        cls,
        from_date: date,
        to_date: date,
        items: tuple[EarningsCalendarItem, ...],
    ) -> "EarningsCalendar":
        by_day: dict[date, list[EarningsCalendarItem]] = {}
        for item in items:
            by_day.setdefault(item.report_date, []).append(item)
        days = tuple(
            EarningsCalendarDay(
                date=day,
                items=tuple(sorted(by_day[day], key=lambda i: i.ticker)),
            )
            for day in sorted(by_day)
        )
        return cls(from_date=from_date, to_date=to_date, days=days)
