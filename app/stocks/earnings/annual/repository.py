"""Abstract persistence port for the annual-earnings slice.

The interface the use cases depend on — Dependency Inversion for storage. A use case is
handed an ``AnnualEarningsRepository`` and never knows whether it's backed by SQLAlchemy,
an in-memory fake (tests), or anything else; it just calls these methods. The concrete
SQLAlchemy implementation lives in ``db_repository.py``, over the models and queries in
``models.py``.

A *Repository*, not a *Provider*: the rows are slow-moving reference data refreshed out of
band (the cron endpoint) and lazily on a miss, not a live feed. Caching yfinance this way
keeps the endpoint off Yahoo, which rate-limits data-centre IPs.
"""

from abc import ABC, abstractmethod
from typing import NamedTuple

from app.stocks.earnings.annual.entities import AnnualEarningsTimeline


class RefreshTarget(NamedTuple):
    """A stored symbol due for a refresh, paired with the name to carry through.

    What ``refresh_targets`` hands the sync use case: the symbol to re-fetch and the
    display name already on its ``stocks`` row, so a nameless refresh doesn't drop a known
    company name when it re-stores the rows.
    """

    symbol: str
    name: str | None


class AnnualEarningsRepository(ABC):
    """A persistent store for a stock's per-year earnings timeline.

    The database-backed companion to the live ``AnnualEarningsProvider``: the provider hits
    the vendor (yfinance), this caches the result so the endpoint rarely does.
    """

    @abstractmethod
    def get(self, symbol: str) -> AnnualEarningsTimeline | None:
        """Return the stored timeline for the (already-normalized) symbol, or ``None`` when
        nothing is stored yet. A miss is not an error — it's the gap the read-through cache
        fills from the live source. A stored symbol always has at least one year row, so
        ``None`` unambiguously means "never cached", never "cached but empty"."""
        raise NotImplementedError

    @abstractmethod
    def upsert(
        self, symbol: str, name: str | None, timeline: AnnualEarningsTimeline
    ) -> None:
        """Replace the stored years for the symbol with ``timeline``'s, stamping the fetch
        time. Ensures the parent ``stocks`` row exists, setting its display name when one is
        supplied (never overwriting a known name with ``None``).

        Callers should avoid persisting an empty timeline over a populated one — a transient
        empty result from the live source would otherwise wipe good history.
        """
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        """Return the stocks most in need of a refresh, un-cached first then stalest-fetched,
        each paired with the name on its ``stocks`` row.

        Includes stocks not yet cached, so the out-of-band sync both *seeds* new coverage and
        renews stale rows. ``limit`` caps the batch; ``None`` returns every anchor stock (one
        sweep seeds them all). Lazy fill on first access still covers a symbol between sweeps."""
        raise NotImplementedError
