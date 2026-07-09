"""Application port for the news live source.

The abstraction the read path depends on for a *live* source of a stock's recent
news — implemented by the yfinance adapter and the DB-cache decorator in
``app/stocks/adapters``. Dependency Inversion: the core reads through this
interface, never yfinance directly, so the vendor is swappable and the tests run
offline against a hand-written fake. The *persistence* seam is separate — the
repository port lives in ``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.news.entities import StockNews


class NewsProvider(ABC):
    """A gateway for a stock's recent news headlines."""

    @abstractmethod
    def get_news(self, symbol: str) -> StockNews:
        """Return recent news for the (already-normalized) symbol, newest article first.

        Returns an ``is_empty`` run when the source carries no news for the symbol —
        "no news" is not an error for this best-effort feature.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError
