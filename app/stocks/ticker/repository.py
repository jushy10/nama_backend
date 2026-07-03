"""Abstract persistence port for the ticker slice.

The interface the use case depends on — Dependency Inversion for storage. The slice
owns no table of its own; what it persists is two anchor-level facts on the shared
``stocks`` row: the company display name and the listing exchange. Neither
effectively ever changes (a rebrand is about as rare as a relisting), so the card
learns each **once** — from its vendor, on the first view of a ticker that lacks it —
and serves it from the DB forever after: a read-through with no TTL, the same
freshness stance as the earnings caches, minus the cron (nothing to keep current).

A *Repository*, not a *Provider*: it fronts our own database, not a vendor. The
concrete SQLAlchemy implementation lives in ``db_repository.py``, over the shared
anchor model in ``app/stocks/stocks/models.py``.
"""

from abc import ABC, abstractmethod
from typing import NamedTuple


class StoredTickerFacts(NamedTuple):
    """The anchor-level facts the card serves DB-first — per-field ``None`` for
    whatever the row hasn't learned yet (or when there's no row at all)."""

    name: str | None
    exchange: str | None


class TickerRepository(ABC):
    """A persistent store for the anchor-level facts the ticker card serves."""

    @abstractmethod
    def get_facts(self, symbol: str) -> StoredTickerFacts:
        """Return the stored name + exchange for the (already-normalized) symbol,
        each ``None`` when not known yet. A miss is not an error — it's the gap
        the lazy fill closes from the vendors."""
        raise NotImplementedError

    @abstractmethod
    def save_name(self, symbol: str, name: str) -> None:
        """Record the symbol's company display name, creating the ``stocks`` row if
        absent and never clobbering a value already stored."""
        raise NotImplementedError

    @abstractmethod
    def save_exchange(self, symbol: str, exchange: str) -> None:
        """Record the symbol's listing exchange, creating the ``stocks`` row if
        absent and never clobbering a value already stored."""
        raise NotImplementedError
