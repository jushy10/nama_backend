"""Application port for the recommendations live source.

The abstraction the read path depends on for a *live* source of analyst
recommendation trends — implemented by the yfinance adapter and the DB-cache
decorator in ``app/stocks/adapters``. Dependency Inversion: the core reads
through this interface, never yfinance directly, so the vendor is swappable and
the tests run offline against a hand-written fake. The *persistence* seam is
separate — the repository port lives in ``repository.py``.
"""

from abc import ABC, abstractmethod

from app.stocks.recommendations.entities import AnalystRecommendations


class RecommendationProvider(ABC):
    """A gateway for a stock's analyst recommendation trends.

    The sell-side buy/hold/sell split, by month — the "what does the street
    think?" forward view, from a ratings vendor rather than the price feed.
    """

    @abstractmethod
    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        """Return recommendation trends for the (already-normalized) symbol,
        newest snapshot first.

        Returns an ``is_empty`` run when no analyst covers the symbol — "no
        coverage" is not an error for this best-effort feature.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError
