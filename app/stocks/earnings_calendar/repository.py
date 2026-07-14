"""Abstract persistence port for the earnings-calendar slice.

The one read the use case depends on — Dependency Inversion for storage. The use case is
handed an ``EarningsCalendarRepository`` and never knows it's a join across
``stock_quarterly_earnings`` and ``stocks``; it just asks for the upcoming reports in a
window. The concrete SQLAlchemy implementation is in ``db_repository.py``.

A *Repository*, not a *Provider*: it reads scheduled dates other slices' syncs already stored,
never a live vendor — so a calendar read is one indexed DB query, never a Yahoo round-trip.
"""

from abc import ABC, abstractmethod
from datetime import date

from app.stocks.earnings_calendar.entities import EarningsCalendarItem


class EarningsCalendarRepository(ABC):
    """A read of upcoming scheduled earnings across the screened universe."""

    @abstractmethod
    def upcoming(
        self, from_date: date, to_date: date, limit: int
    ) -> list[EarningsCalendarItem]:
        """Scheduled upcoming reports with a ``report_date`` in ``[from_date, to_date]``
        (inclusive), ordered by date then ticker, capped at ``limit``.

        "Upcoming" means a quarter that hasn't reported yet (its ``eps_actual`` is still
        unset) but carries a scheduled announcement date — so a company that has already
        posted actuals for the quarter is excluded. Each item is joined to its company name
        and sector off the ``stocks`` anchor. Empty when nothing is scheduled in the window."""
        raise NotImplementedError
