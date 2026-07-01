"""Application port for the annual-earnings live source.

The abstraction the read path depends on for a *live* source of per-year earnings —
implemented by the yfinance adapter and the DB-cache decorator in ``app/stocks/adapters``.
Dependency Inversion: the core reads through this interface, never yfinance directly, so
the vendor is swappable and the tests run offline against a hand-written fake. The
*persistence* seam is separate — the repository port lives in ``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.earnings.annual.entities import AnnualEarningsTimeline


class AnnualEarningsProvider(ABC):
    """A gateway for a stock's recent reported fiscal years and its upcoming ones."""

    @abstractmethod
    def get_annual_earnings(self, symbol: str) -> AnnualEarningsTimeline:
        """Return the per-year earnings timeline for the (already-normalized) symbol.

        Returns an ``is_empty`` timeline (no years) when the source covers the symbol with
        no earnings — "no data" is not an error for this best-effort feature.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError
