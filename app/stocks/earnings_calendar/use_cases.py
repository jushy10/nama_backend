"""Application use case for the earnings calendar.

``GetEarningsCalendar`` — the read path: normalize/clamp the requested ``[from, to]`` window,
pull the upcoming scheduled reports across the universe through the repository, and fold them
into per-day groups. Pure orchestration over the one port, so it runs offline in tests against
a hand-written fake and knows nothing of SQLAlchemy or HTTP.

Two guardrails keep the read bounded (the spec's "paginated/capped"): the window is **clamped**
to at most ``MAX_WINDOW_DAYS`` (a too-wide request is narrowed, not rejected), and the row read
is **capped** at ``MAX_ITEMS`` so even a dense window returns a sane page. An inverted window
(``to`` before ``from``) is the one hard error — there's nothing sensible to clamp it to.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.stocks.earnings_calendar.entities import EarningsCalendar
from app.stocks.earnings_calendar.repository import EarningsCalendarRepository


class GetEarningsCalendar:
    """Use case: the upcoming earnings calendar over a (clamped) date window."""

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
        self, repository: EarningsCalendarRepository, *, today=None
    ) -> None:
        self._repository = repository
        # Injectable clock keeps the default window deterministic in tests.
        self._today = today or (lambda: datetime.now(timezone.utc).date())

    def execute(
        self, from_date: date | None = None, to_date: date | None = None
    ) -> EarningsCalendar:
        """Read the calendar over ``[from_date, to_date]``.

        ``from_date`` defaults to today and ``to_date`` to ``DEFAULT_WINDOW_DAYS`` past the
        start. The window is clamped to ``MAX_WINDOW_DAYS`` and the read capped at
        ``MAX_ITEMS``. Raises ``ValueError`` when ``to_date`` precedes ``from_date``."""
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
