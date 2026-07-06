"""Application port for the ETF live source (the screener).

The abstraction the sync use case depends on for a *live* screen of the US ETF market —
Yahoo's curated "top ETFs" set. Implemented by the yfinance adapter in ``app/stocks/adapters``.
Dependency Inversion — the core screens through this interface, never a vendor directly, so
the source is swappable and the tests run offline against a hand-written fake. The
*persistence* seam is separate — the repository port lives in ``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.etfs.entities import ScreenedEtf


class EtfScreener(ABC):
    """A gateway for screening the US ETF market (the top funds by the source's ranking)."""

    @abstractmethod
    def screen(self) -> tuple[ScreenedEtf, ...]:
        """Return the screened set of top US ETFs.

        One bulk read of the curated set the sync persists — it takes no criteria of its own
        (the vendor's "top ETFs" screen carries its own filter). Order is unspecified; the
        read side sorts, so callers must not rely on it.

        Raises:
            StockDataUnavailable: the upstream screen failed. The sync treats this as a lost
                round — it skips the write rather than acting on a partial or empty result — so
                an adapter must raise here rather than return a half-populated screen.
        """
        raise NotImplementedError
