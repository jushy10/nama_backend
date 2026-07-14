"""Entities: the market-wide earnings calendar (upcoming reports grouped by day).

Slice-local domain objects, pure and vendor-agnostic — stdlib only. They model the calendar
as a flat list of scheduled reports folded into per-day groups: each ``EarningsCalendarItem``
is one company's upcoming report, ``EarningsCalendarDay`` is the reports scheduled for one
date, and ``EarningsCalendar`` is the ordered run of days over the requested window.

The grouping and ordering (days ascending, companies alphabetical within a day) are *facts
about the calendar*, so they live here in :meth:`EarningsCalendar.build`, not in the use case
— the use case just supplies the window and the flat rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class EarningsCalendarItem:
    """One company's upcoming earnings report on a scheduled date.

    ``report_date`` is when the company is expected to report — the scheduled announcement
    date the quarterly slice stores for its not-yet-reported quarters. Our data is
    date-granular (no intraday before-open / after-close session), so ``report_date`` is the
    whole of the timing signal; the presenter surfaces it per item as ``when``. ``name`` and
    ``sector`` come from the ``stocks`` anchor and may be ``None`` for a thinly-known symbol.
    """

    ticker: str
    name: str | None
    sector: str | None
    report_date: date


@dataclass(frozen=True)
class EarningsCalendarDay:
    """The companies scheduled to report on one calendar date, alphabetical by ticker."""

    date: date
    items: tuple[EarningsCalendarItem, ...]


@dataclass(frozen=True)
class EarningsCalendar:
    """The upcoming earnings calendar over a window: days ascending, each with its reports.

    ``from_date`` / ``to_date`` echo the (clamped) window the calendar was read over; ``days``
    are the days *that have at least one scheduled report* (a quiet day simply doesn't appear).
    """

    from_date: date
    to_date: date
    days: tuple[EarningsCalendarDay, ...]

    @property
    def count(self) -> int:
        """Total scheduled reports across every day in the window."""
        return sum(len(day.items) for day in self.days)

    @classmethod
    def build(
        cls,
        from_date: date,
        to_date: date,
        items: tuple[EarningsCalendarItem, ...],
    ) -> "EarningsCalendar":
        """Fold flat, date-ordered items into per-day groups.

        Groups by ``report_date`` into ``EarningsCalendarDay``s ordered oldest→newest, each
        day's items sorted by ticker for a stable, deterministic read. An empty input yields a
        calendar with no days (a valid, quiet window)."""
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
