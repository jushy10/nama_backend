"""Abstract persistence port for the insider-transactions slice.

The interface the read path and the out-of-band sync depend on for storage — Dependency
Inversion, so the read-through cache decorator, the ``SyncInsiderTransactions`` use case, and the
tests are handed an ``InsiderTransactionsRepository`` and never know whether it's backed by
SQLAlchemy or an in-memory fake. The concrete SQLAlchemy implementation lives in
``db_repository.py``, over the models and queries in ``models.py``.

Like its sibling cache slices (revenue-segments / news / recommendations) this is a plain
**read-through** cache: a populated symbol is served straight from the DB at any age, and a
**weekly out-of-band cron** (``SyncInsiderTransactions``) keeps the stored rows current. The cron
walks ``refresh_targets`` — un-cached stocks first (so it also *seeds* new coverage), then the
least-recently-refreshed — and renews each from SEC EDGAR (which asks automated clients to stay
under ~10 requests/second), so a user request never waits on the multi-request-per-symbol filing
walk. ``fetched_at`` is the staleness key the sweep orders by; the read path never checks it.
"""

from abc import ABC, abstractmethod
from typing import NamedTuple

from app.stocks.insider_transactions.entities import InsiderActivity


class RefreshTarget(NamedTuple):
    """A stored symbol due for a refresh, paired with the name to carry through.

    What ``refresh_targets`` hands the sync use case: the symbol to re-fetch and the display
    name already on its ``stocks`` row, so a nameless refresh doesn't drop a known company name
    when it re-stores the rows.
    """

    symbol: str
    name: str | None


class InsiderTransactionsRepository(ABC):
    """A persistent store for a stock's insider (Form 4) transactions.

    The database-backed companion to the live ``InsiderTransactionsProvider``: the provider hits
    the source (SEC EDGAR), this caches the result so the endpoint rarely does.
    """

    @abstractmethod
    def get(self, symbol: str) -> InsiderActivity | None:
        """Return the stored activity for the (already-normalized) symbol, or ``None`` when
        nothing is stored yet. A stored symbol always has at least one transaction row, so
        ``None`` unambiguously means "never cached", never "cached but empty"."""
        raise NotImplementedError

    @abstractmethod
    def upsert(self, symbol: str, name: str | None, activity: InsiderActivity) -> None:
        """Merge ``activity``'s transactions into the store, stamping the fetch time.

        **Insert-only** by ``(accession_number, line_index)`` — a filed transaction is a frozen
        fact, so a refresh adds only transactions not already stored and never rewrites an
        existing row (like the rating-changes slice). The accumulated feed is **pruned** to the
        newest N transactions per stock so the far-higher-volume history stays bounded (like the
        news slice). Every stored row's fetch stamp is refreshed to the current time (even when
        the fetch brought no new transactions) so the sweep's staleness order treats the whole
        feed as freshly confirmed. Ensures the parent ``stocks`` row exists, setting its display
        name when one is supplied (never overwriting a known name with ``None``).
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
