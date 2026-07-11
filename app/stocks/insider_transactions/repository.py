"""Abstract persistence port for the insider-transactions slice.

The interface the read path depends on for storage — Dependency Inversion, so the TTL cache
decorator and the tests are handed an ``InsiderTransactionsRepository`` and never know whether
it's backed by SQLAlchemy or an in-memory fake. The concrete SQLAlchemy implementation lives in
``db_repository.py``, over the models and queries in ``models.py``.

Unlike the sibling cache slices there is **no ``refresh_targets`` and no ``Sync*`` use case**:
this slice has no out-of-band cron. Freshness rides entirely on the TTL read-through cache
(``DbCachedInsiderTransactionsProvider``), which re-fetches a stock on read once its stored rows
age past the TTL — hence ``latest_fetched_at``, the freshness stamp the decorator checks. Caching
SEC this way keeps the endpoint off EDGAR (which asks automated clients to stay under ~10
requests/second) for all but the first — and, past the TTL, the occasional — view of a symbol.
"""

from abc import ABC, abstractmethod
from datetime import datetime

from app.stocks.insider_transactions.entities import InsiderActivity


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
    def latest_fetched_at(self, symbol: str) -> datetime | None:
        """The most recent fetch stamp among the symbol's stored rows, or ``None`` when nothing
        is stored. The TTL cache compares this against its clock to decide serve-stored vs.
        re-fetch. The upsert refreshes every row's stamp on each fetch, so the newest stamp is
        the last time the symbol was actually confirmed against the source."""
        raise NotImplementedError

    @abstractmethod
    def upsert(self, symbol: str, name: str | None, activity: InsiderActivity) -> None:
        """Merge ``activity``'s transactions into the store, stamping the fetch time.

        **Insert-only** by ``(accession_number, line_index)`` — a filed transaction is a frozen
        fact, so a refresh adds only transactions not already stored and never rewrites an
        existing row (like the rating-changes slice). The accumulated feed is **pruned** to the
        newest N transactions per stock so the far-higher-volume history stays bounded (like the
        news slice). Every stored row's fetch stamp is refreshed to the current time (even when
        the fetch brought no new transactions) so the TTL cache treats the whole feed as freshly
        confirmed. Ensures the parent ``stocks`` row exists, setting its display name when one is
        supplied (never overwriting a known name with ``None``).
        """
        raise NotImplementedError
