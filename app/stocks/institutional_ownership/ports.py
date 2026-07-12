"""Application port for the institutional-ownership live source.

The abstraction the read path depends on for a *live* source of a stock's institutional
ownership — implemented by the yfinance adapter and the DB-cache decorator in
``app/stocks/adapters``. Dependency Inversion: the core reads through this interface, never
yfinance directly, so the vendor is swappable (a future SEC 13F reverse-index adapter could
implement the same port) and the tests run offline against a hand-written fake. The *persistence*
seam is separate — the repository port lives in ``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.institutional_ownership.entities import InstitutionalOwnership


class InstitutionalOwnershipProvider(ABC):
    """A gateway for a stock's institutional ownership — its top 13F holders and the summary
    breakdown."""

    @abstractmethod
    def get_institutional_ownership(self, symbol: str) -> InstitutionalOwnership:
        """Return the institutional ownership for the (already-normalized) symbol.

        Returns an ``is_empty`` ownership when the source covers the symbol but carries no
        institutional holders — "no institutional coverage" is not an error for this best-effort
        feature.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed (transport / bad response / block).
        """
        raise NotImplementedError
