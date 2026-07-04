"""Application port for the index-membership live source.

The abstraction the sync use case depends on for a live read of which stocks belong to the
tracked indices (the S&P 500 and Nasdaq-100). Implemented by the Finnhub adapter in
``app/stocks/adapters``. Dependency Inversion — the core reads through this interface, never a
vendor directly, so the source is swappable (Finnhub today, an ETF-holdings feed or another
vendor tomorrow) and the tests run offline against a hand-written fake. The *persistence*
seam is separate — the repository port lives in ``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.index_membership.entities import IndexMembershipSnapshot


class IndexMembershipSource(ABC):
    """A gateway for reading current index membership."""

    @abstractmethod
    def fetch(self) -> IndexMembershipSnapshot:
        """Return the current membership of the tracked indices as sets of tickers.

        One bulk read per index — the sets the sync reconciles onto the anchor. Order is
        irrelevant (they are sets).

        Raises:
            StockDataUnavailable: nothing usable could be fetched (every index failed). A
                *single* index failing is not an error — it comes back as an empty set, and
                the use case's plausibility floor skips it rather than wiping its members. So an
                adapter raises here only when it has nothing at all to offer.
        """
        raise NotImplementedError
