from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.domains.financials.earnings_calendar.entities import EarningsCalendar
from app.domains.financials.earnings_calendar.interfaces import EarningsCalendarRepositoryAdapter


class GetEarningsCalendar:
    # Default window when the caller gives no ``to`` — two weeks ahead is a readable
    # "what's coming up" horizon.
    DEFAULT_WINDOW_DAYS = 14
    # The widest window a single read spans; a broader request is clamped to this (a quarter
    # of scheduled dates is already well past a useful calendar view).
    MAX_WINDOW_DAYS = 92
    # Row cap for one read — comfortably above a dense earnings week across the ≥$1B universe,
    # but a hard ceiling so a pathological window can't return an unbounded page.
    MAX_ITEMS = 2000

    def __init__(
        self, repository: EarningsCalendarRepositoryAdapter, *, today=None
    ) -> None:
        self._repository = repository
        # Injectable clock keeps the default window deterministic in tests.
        self._today = today or (lambda: datetime.now(timezone.utc).date())

    def execute(
        self, from_date: date | None = None, to_date: date | None = None
    ) -> EarningsCalendar:
        start = from_date or self._today()
        end = to_date or (start + timedelta(days=self.DEFAULT_WINDOW_DAYS))
        if end < start:
            raise ValueError("'to' must not be before 'from'.")
        # Clamp an over-wide window rather than rejecting it — a broad ask still gets a
        # (narrowed) answer.
        max_end = start + timedelta(days=self.MAX_WINDOW_DAYS)
        if end > max_end:
            end = max_end
        items = tuple(self._repository.upcoming(start, end, self.MAX_ITEMS))
        return EarningsCalendar.build(start, end, items)
