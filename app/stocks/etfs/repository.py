"""Abstract persistence ports for the ETF slice.

Dependency Inversion for storage: the use cases are handed a repository and never know whether
it's backed by SQLAlchemy or an in-memory fake (tests). The concrete SQLAlchemy implementations
live in ``db_repository.py``.

Two ports, split by capability (the ``CLAUDE.md`` "one port per capability" rule): the write
side ``EtfRepository`` the sync uses, and the read side ``EtfSearchRepository`` the
``GET /stocks/etfs`` search uses. Both front the slice's own ``etfs`` table — unlike the stock
``universe`` slice, which is table-less and writes onto the shared ``stocks`` anchor; an ETF is
not a company, so it gets its own table rather than polluting the stock universe.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.stocks.etfs.entities import EtfSearchCriteria, EtfSearchPage, ScreenedEtf


@dataclass(frozen=True)
class EtfSyncCounts:
    """The row-level outcome of one screen upsert: funds newly inserted (``added``) and
    existing rows refreshed in place (``updated``).

    The sync is **additive** — it never removes an ETF. A fund that later drops out of the top
    screen keeps its last-screened facts rather than being deleted, so there is no ``removed``
    count. (The set is stable at ~540 names, so lingering staleness is a minor, accepted
    trade-off for never wiping the table on a bad screen.)
    """

    added: int
    updated: int


class EtfRepository(ABC):
    """A persistent store for the screened ETF set, refreshed by the sync — the ``etfs`` table."""

    @abstractmethod
    def upsert_screen(self, etfs: tuple[ScreenedEtf, ...]) -> EtfSyncCounts:
        """Upsert every screened ETF into the ``etfs`` table and return the per-row counts.

        For each: create the row if absent, fill ticker/name/exchange when missing (never
        clobbering a settled value), and set/refresh the screen figures
        (``net_assets``/``expense_ratio``/``ytd_return``) plus the last-screen stamp. Additive:
        ETFs absent from the screen are left untouched (no delete). Commits its own write.
        """
        raise NotImplementedError


class EtfSearchRepository(ABC):
    """A read-only view over the stored ETF set — what the ``GET /stocks/etfs`` search reads.

    Read-only by design: the search never writes (the sync owns every column it reads), so this
    is a separate, small port the write-side ``EtfRepository`` doesn't share.
    """

    @abstractmethod
    def search(self, criteria: EtfSearchCriteria) -> EtfSearchPage:
        """Return the page of ETFs matching ``criteria`` plus the total match count.

        Applies the free-text filter when set (substring on name *or* ticker), orders by the
        requested sort with a stable ``ticker`` tiebreak and nulls last, and cuts the
        ``limit``/``offset`` window. ``total`` is the pre-window count, for the client's pager.
        An empty result is not an error — it's a page with no rows.
        """
        raise NotImplementedError
