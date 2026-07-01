"""Application port for the quarterly-earnings live source.

The abstraction the read path depends on for a *live* source of per-quarter earnings —
implemented by the yfinance adapter and the DB-cache decorator in ``app/stocks/adapters``.
Dependency Inversion: the core reads through this interface, never yfinance directly, so
the vendor is swappable and the tests run offline against a hand-written fake. The
*persistence* seam is separate — the repository port lives in ``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline


class QuarterlyEarningsProvider(ABC):
    """A gateway for a stock's recent reported quarters and its upcoming ones."""

    @abstractmethod
    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        """Return the per-quarter earnings timeline for the (already-normalized) symbol.

        Returns an ``is_empty`` timeline (no quarters) when the source covers the symbol
        with no earnings — "no data" is not an error for this best-effort feature.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError
