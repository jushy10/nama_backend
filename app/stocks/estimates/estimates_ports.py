"""Application ports for the analyst-estimates slice.

The abstractions the estimates use cases depend on. They live in the slice — rather
than the shared ``app.stocks.ports`` — so the feature owns its own seams. Same
Dependency Inversion as everywhere else: the use cases depend on these interfaces and
the adapter layer (FMP, the DB-cache decorator, the SQL repository) implements them.
The core never imports a vendor; the vendor imports the core. It's also what lets the
tests run offline against hand-written fakes.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import NamedTuple

from app.stocks.entities import AnalystEstimates


class AnalystEstimatesProvider(ABC):
    """A gateway for a stock's forward analyst consensus estimates.

    Forward EPS/revenue expectations come from a sell-side estimates vendor — not
    the price feed or company filings — so this carries consensus *estimates*, never
    reported actuals. Best-effort enrichment on the stock snapshot (it backs the
    forward P/E and forward P/S), so a failure here must not sink the price response.
    """

    @abstractmethod
    def get_estimates(self, symbol: str) -> AnalystEstimates:
        """Return forward consensus estimates for the (already-normalized) symbol.

        Returns an ``is_empty`` ``AnalystEstimates`` (all ``None``) when the source
        covers no forward fiscal year for the symbol — "no data" is not an error for
        best-effort enrichment.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class CachedEstimates(NamedTuple):
    """A stored estimate plus when it was last fetched.

    The repository's read shape: the entity the use case wants, paired with the
    fetch timestamp so the cache decorator can judge staleness in one query
    rather than two.
    """

    estimates: AnalystEstimates
    fetched_at: datetime


class RefreshTarget(NamedTuple):
    """A stored symbol due for a refresh, paired with the name to carry through.

    What ``refresh_targets`` hands the sync use case: the symbol to re-fetch and the
    display name already on its ``stocks`` row, so a nameless refresh doesn't drop a
    known company name when it re-stores the row.
    """

    symbol: str
    name: str | None


class AnalystEstimatesRepository(ABC):
    """A persistent store for a stock's forward analyst estimates.

    The database-backed companion to ``AnalystEstimatesProvider``: the live
    provider hits the vendor (FMP), this caches the result so the endpoint rarely
    does. A *Repository*, not a *Provider — the rows are slow-moving reference data
    refreshed out of band (the cron endpoint) and lazily on a miss, not a live feed.
    Caching the vendor keeps the endpoint under FMP's ~250-calls/day free quota.
    """

    @abstractmethod
    def get(self, symbol: str) -> CachedEstimates | None:
        """Return the stored estimates for the (already-normalized) symbol, or
        ``None`` when nothing is stored yet. A miss is not an error — it's the gap
        the cache decorator fills from the live source."""
        raise NotImplementedError

    @abstractmethod
    def upsert(self, symbol: str, name: str | None, estimates: AnalystEstimates) -> None:
        """Insert or replace the stored estimates for the symbol, stamping the
        fetch time. Ensures the parent ``stocks`` row exists, setting its display
        name when one is supplied (never overwriting a known name with ``None``)."""
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int) -> list[RefreshTarget]:
        """Return up to ``limit`` stored symbols most in need of a refresh,
        stalest-fetched first, each paired with the name on its ``stocks`` row.

        The out-of-band sync walks these to renew the rows users actually view while
        staying within the vendor's quota; symbols never stored (hence never viewed)
        aren't returned — those are filled lazily on first access instead."""
        raise NotImplementedError
