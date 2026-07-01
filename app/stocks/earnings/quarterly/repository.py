"""Abstract persistence port for the quarterly-earnings slice.

The interface the use cases depend on — Dependency Inversion for storage. A use case is
handed a ``QuarterlyEarningsRepository`` and never knows whether it's backed by
SQLAlchemy, an in-memory fake (tests), or anything else; it just calls these methods. The
concrete SQLAlchemy implementation lives in ``db_repository.py``, over the models and
queries in ``models.py``.

A *Repository*, not a *Provider*: the rows are slow-moving reference data refreshed out
of band (the cron endpoint) and lazily on a miss, not a live feed. Caching yfinance this
way keeps the endpoint off Yahoo, which rate-limits data-centre IPs.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import NamedTuple

from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline


class CachedQuarterlyEarnings(NamedTuple):
    """A stored timeline plus when it was last fetched.

    The repository's read shape: the entity the use case wants, paired with the fetch
    timestamp so the cache decorator can judge staleness in one query rather than two.
    """

    timeline: QuarterlyEarningsTimeline
    fetched_at: datetime


class RefreshTarget(NamedTuple):
    """A stored symbol due for a refresh, paired with the name to carry through.

    What ``refresh_targets`` hands the sync use case: the symbol to re-fetch and the
    display name already on its ``stocks`` row, so a nameless refresh doesn't drop a
    known company name when it re-stores the rows.
    """

    symbol: str
    name: str | None


class QuarterlyEarningsRepository(ABC):
    """A persistent store for a stock's per-quarter earnings timeline.

    The database-backed companion to the live ``QuarterlyEarningsProvider``: the
    provider hits the vendor (yfinance), this caches the result so the endpoint rarely
    does.
    """

    @abstractmethod
    def get(self, symbol: str) -> CachedQuarterlyEarnings | None:
        """Return the stored timeline for the (already-normalized) symbol, or ``None``
        when nothing is stored yet. A miss is not an error — it's the gap the cache
        decorator fills from the live source."""
        raise NotImplementedError

    @abstractmethod
    def upsert(
        self, symbol: str, name: str | None, timeline: QuarterlyEarningsTimeline
    ) -> None:
        """Replace the stored quarters for the symbol with ``timeline``'s, stamping the
        fetch time. Ensures the parent ``stocks`` row exists, setting its display name
        when one is supplied (never overwriting a known name with ``None``).

        Callers should avoid persisting an empty timeline over a populated one — a
        transient empty result from the live source would otherwise wipe good history.
        """
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int) -> list[RefreshTarget]:
        """Return up to ``limit`` stored symbols most in need of a refresh,
        stalest-fetched first, each paired with the name on its ``stocks`` row.

        The out-of-band sync walks these to renew the rows users actually view while
        staying gentle on the vendor; symbols never stored (hence never viewed) aren't
        returned — those are filled lazily on first access instead."""
        raise NotImplementedError
