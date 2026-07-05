"""Abstract persistence port for the ticker slice.

The interface the use case depends on — Dependency Inversion for storage. The slice
owns no table of its own; what it reads are anchor-level facts on the shared
``stocks`` row. Two of them it also **fills** — the company display name and the
listing exchange: neither effectively ever changes (a rebrand is about as rare as a
relisting), so the card learns each **once** (from its vendor, on the first view of a
ticker that lacks it) and serves it from the DB forever after — a read-through with no
TTL, the same freshness stance as the earnings caches, minus the cron. The rest it
only **reads**: ``market_cap`` / ``sector`` / ``industry`` are the universe screen's
facts and ``revenue_growth_yoy`` / ``eps_growth_yoy`` the annual slice's trailing
snapshot, both denormalized onto the anchor by their own syncs — the card just serves
whatever those have written (``None`` until they reach the stock), never filling them
itself.

A *Repository*, not a *Provider*: it fronts our own database, not a vendor. The
concrete SQLAlchemy implementation lives in ``db_repository.py``, over the shared
anchor model in ``app/stocks/stocks/models.py``.
"""

from abc import ABC, abstractmethod
from typing import NamedTuple


class StoredTickerFacts(NamedTuple):
    """The anchor-level facts the card serves DB-first — per-field ``None`` for
    whatever the row hasn't learned yet (or when there's no row at all).

    ``name`` / ``exchange`` are the fill-once identity facts the card lazily learns;
    the rest are read-only reflections of other slices' writes onto the anchor —
    ``market_cap`` / ``sector`` / ``industry`` from the universe screen, and
    ``revenue_growth_yoy`` / ``eps_growth_yoy`` (percent, consensus-basis EPS) the
    annual slice's latest trailing year-over-year snapshot."""

    name: str | None = None
    exchange: str | None = None
    market_cap: float | None = None
    sector: str | None = None
    industry: str | None = None
    revenue_growth_yoy: float | None = None
    eps_growth_yoy: float | None = None


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
