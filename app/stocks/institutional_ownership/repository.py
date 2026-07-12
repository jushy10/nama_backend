"""Abstract persistence port for the institutional-ownership slice.

The interface the read path and the sync use case depend on for storage — Dependency Inversion, so
the DB-cache decorator, the sync, and the tests are handed an ``InstitutionalOwnershipRepository``
and never know whether it's backed by SQLAlchemy or an in-memory fake. The concrete SQLAlchemy
implementation lives in ``db_repository.py``, over the models and queries in ``models.py``.

A *Repository*, not a *Provider*: the rows are cached holdings refreshed out of band (the cron
endpoint) and lazily on a miss, not a live feed. Caching yfinance this way keeps the endpoint off
Yahoo, which rate-limits and blocks data-centre IPs.
"""

from abc import ABC, abstractmethod
from typing import NamedTuple

from app.stocks.institutional_ownership.entities import InstitutionalOwnership


class RefreshTarget(NamedTuple):
    """A stored symbol due for a refresh, paired with the name to carry through.

    What ``refresh_targets`` hands the sync use case: the symbol to re-fetch and the display name
    already on its ``stocks`` row, so a nameless refresh doesn't drop a known company name."""

    symbol: str
    name: str | None


class InstitutionalOwnershipRepository(ABC):
    """A persistent store for a stock's institutional ownership.

    The database-backed companion to the live ``InstitutionalOwnershipProvider``: the provider hits
    the vendor (yfinance), this caches the result so the endpoint rarely does.
    """

    @abstractmethod
    def get(self, symbol: str) -> InstitutionalOwnership | None:
        """Return the stored ownership for the (already-normalized) symbol, or ``None`` when
        nothing is stored yet. A stored symbol always has at least one holder row, so ``None``
        unambiguously means "never cached", never "cached but empty"."""
        raise NotImplementedError

    @abstractmethod
    def upsert(
        self, symbol: str, name: str | None, ownership: InstitutionalOwnership
    ) -> None:
        """Merge ``ownership`` into the store, stamping the fetch time.

        *Merge*, not rewrite — like the news/recommendations repositories: the holders feed
        **replaces the snapshots it re-served** (by holder-type + reported quarter) and leaves
        earlier reported quarters intact, so the store accumulates a *history* of holdings across
        quarters even though the source serves only the latest. The accumulated feed is **pruned**
        to the newest N holder rows per stock so it stays bounded. The ownership **breakdown** is a
        single current snapshot, so it's **overwritten** each refresh. Ensures the parent ``stocks``
        row exists, setting its display name when one is supplied (never overwriting a known name
        with ``None``).
        """
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        """Return the stocks most in need of a refresh, un-cached first then
        least-recently-refreshed, each paired with the name on its ``stocks`` row.

        Includes stocks not yet cached, so the out-of-band sync both *seeds* new coverage and
        renews stale rows. ``limit`` caps the batch; ``None`` returns every anchor stock. Staleness
        reads the *newest* fetch stamp among a stock's holder rows (the merge keeps old quarters'
        stamps forever, so the min would pin a long-cached stock permanently stale)."""
        raise NotImplementedError
