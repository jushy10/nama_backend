"""Abstract persistence port for the revenue-segments slice.

The interface the use cases depend on — Dependency Inversion for storage. A use case is
handed a ``RevenueSegmentsRepository`` and never knows whether it's backed by SQLAlchemy, an
in-memory fake (tests), or anything else; it just calls these methods. The concrete
SQLAlchemy implementation lives in ``db_repository.py``, over the models and queries in
``models.py``.

A *Repository*, not a *Provider*: the rows are slow-moving reference data (a company's segment
revenue changes about once a year, on a filing) refreshed out of band (the cron endpoint) and
lazily on a miss, not a live feed. Caching SEC this way keeps the endpoint off EDGAR — which
asks automated clients to stay under 10 requests/second — and off the multi-request-per-symbol
filing walk.
"""

from abc import ABC, abstractmethod
from typing import NamedTuple

from app.stocks.revenue_segments.entities import RevenueSegmentation


class RefreshTarget(NamedTuple):
    """A stored symbol due for a refresh, paired with the name to carry through.

    What ``refresh_targets`` hands the sync use case: the symbol to re-fetch and the display
    name already on its ``stocks`` row, so a nameless refresh doesn't drop a known company name
    when it re-stores the rows.
    """

    symbol: str
    name: str | None


class RevenueSegmentsRepository(ABC):
    """A persistent store for a company's revenue disaggregation.

    The database-backed companion to the live ``RevenueSegmentsProvider``: the provider hits the
    source (SEC EDGAR), this caches the result so the endpoint rarely does.
    """

    @abstractmethod
    def get(self, symbol: str) -> RevenueSegmentation | None:
        """Return the stored segmentation for the (already-normalized) symbol, or ``None`` when
        nothing is stored yet. A miss is not an error — it's the gap the read-through cache
        fills from the live source. A stored symbol always has at least one segment row, so
        ``None`` unambiguously means "never cached", never "cached but empty"."""
        raise NotImplementedError

    @abstractmethod
    def upsert(
        self, symbol: str, name: str | None, segmentation: RevenueSegmentation
    ) -> None:
        """Merge ``segmentation``'s figures into the store, stamping the fetch time.

        *Merge* by fiscal year, not rewrite — like the recommendations/news repositories and
        unlike the earnings ones: a filing restates only its most-recent ~3 fiscal years, but a
        reported year's disaggregation is a frozen fact, so a refresh replaces exactly the years
        it covers and leaves earlier stored years intact. The store thereby accumulates a longer
        history than any single filing shows, **capped** to the newest N fiscal years per stock
        so it stays bounded. Ensures the parent ``stocks`` row exists, setting its display name
        when one is supplied (never overwriting a known name with ``None``).

        Callers should avoid persisting an empty segmentation over a populated one — a transient
        empty result from the live source would otherwise leave the stored history untouched (an
        empty merge covers no years), but the use case skips it anyway so the refresh stamp advances.
        """
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        """Return the stocks most in need of a refresh, un-cached first then
        least-recently-refreshed, each paired with the name on its ``stocks`` row.

        Includes stocks not yet cached, so the out-of-band sync both *seeds* new coverage and
        renews stale rows. ``limit`` caps the batch; ``None`` returns every anchor stock (one
        sweep seeds them all). Lazy fill on first access still covers a symbol between sweeps."""
        raise NotImplementedError
