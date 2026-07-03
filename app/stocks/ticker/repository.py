"""Abstract persistence port for the ticker slice.

The interface the use case depends on — Dependency Inversion for storage. The slice
owns no table of its own; what it persists is one anchor-level fact: the listing
exchange on the shared ``stocks`` row. A stock's exchange effectively never changes,
so the card fetches it from the price feed **once** (on the first view of a symbol)
and serves it from the DB forever after — a read-through with no TTL, the same
freshness stance as the earnings caches, minus the cron (nothing to keep current).

A *Repository*, not a *Provider*: it fronts our own database, not a vendor. The
concrete SQLAlchemy implementation lives in ``db_repository.py``, over the shared
anchor model in ``app/stocks/stocks/models.py``.
"""

from abc import ABC, abstractmethod


class TickerRepository(ABC):
    """A persistent store for the anchor-level facts the ticker card serves."""

    @abstractmethod
    def get_exchange(self, symbol: str) -> str | None:
        """Return the stored listing exchange for the (already-normalized) symbol,
        or ``None`` when it isn't known yet. A miss is not an error — it's the gap
        the lazy fill closes from the price feed."""
        raise NotImplementedError

    @abstractmethod
    def save_exchange(self, symbol: str, exchange: str) -> None:
        """Record the symbol's listing exchange, creating the ``stocks`` row if
        absent and never clobbering a value already stored."""
        raise NotImplementedError
