"""Application port for the stock-universe live source (the screener).

The abstraction the sync use case depends on for a *live* screen of the US market: every
listed company at/above a market-cap floor. Implemented by the Nasdaq adapter in
``app/stocks/adapters``. Dependency Inversion — the core screens through this interface,
never a vendor directly, so the source is swappable (Nasdaq today, yfinance's ``yf.screen``
tomorrow) and the tests run offline against a hand-written fake. The *persistence* seam is
separate — the repository port lives in ``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.universe.entities import ScreenedStock


class StockScreener(ABC):
    """A gateway for screening the US market by market capitalisation."""

    @abstractmethod
    def screen(self, *, min_market_cap: float) -> tuple[ScreenedStock, ...]:
        """Return every US-listed stock at/above ``min_market_cap`` (whole dollars).

        One bulk read of the whole market, filtered to the floor — the investable
        universe the sync persists. Order is unspecified; callers must not rely on it.

        Raises:
            StockDataUnavailable: the upstream screen failed. The sync treats this as a
                lost round — it skips the reconcile rather than acting on a partial or
                empty result — so an adapter must raise here rather than return a
                half-populated screen.
        """
        raise NotImplementedError
