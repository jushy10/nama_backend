"""Abstract persistence port for the brief slice.

The storage interface the use cases depend on — Dependency Inversion for storage. A use
case is handed a ``MarketBriefRepository`` and never knows whether it's backed by
SQLAlchemy or an in-memory fake (tests); it just calls these methods. The concrete
SQLAlchemy implementation is in ``db_repository.py`` over the model in ``models.py``.

A *Repository*, not a *Provider*: the briefs are our own authored artifacts, written once
per day by the cron and served straight from the store — never a live feed. One row per
calendar date.
"""

from abc import ABC, abstractmethod
from datetime import date

from app.stocks.brief.entities import MarketBrief


class MarketBriefRepository(ABC):
    """A persistent store of the daily market briefs, one row per date."""

    @abstractmethod
    def get(self, brief_date: date) -> MarketBrief | None:
        """Return the brief for ``brief_date``, or ``None`` when none was written that day.

        A miss is not an error — a day with no stored brief (the cron hasn't run, or the
        market data was unavailable) is a clean 404 at the endpoint, not a failure."""
        raise NotImplementedError

    @abstractmethod
    def latest(self) -> MarketBrief | None:
        """Return the most recent brief by date, or ``None`` when the store is empty.

        Backs ``GET /market/brief`` (no date) — the freshest brief we have, whichever day it
        covers, so a gap (a weekend, a missed run) still serves yesterday's read."""
        raise NotImplementedError

    @abstractmethod
    def upsert(self, brief: MarketBrief) -> None:
        """Store ``brief``, replacing any existing row for its ``brief_date``.

        Idempotent by date: re-running a day's generation overwrites that day's row rather
        than accumulating duplicates. Commits its own write so the stored brief is durable
        independent of the caller."""
        raise NotImplementedError
