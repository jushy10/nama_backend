"""Application ports for the analyst-coverage live sources.

The abstractions the read/sync paths depend on for *live* sources — implemented by the
yfinance adapters (and, for recommendations, the DB-cache decorator) in
``app/stocks/adapters``. Dependency Inversion: the core reads through these interfaces,
never yfinance directly, so the vendor is swappable and the tests run offline against
hand-written fakes. The *persistence* seam is separate — the repository ports live in
``repository.py``.

Two capabilities, kept as two small ports: ``RecommendationProvider`` (the monthly
buy/hold/sell trend run, which now also carries the current price-target consensus) and
``RatingChangeProvider`` (the discrete upgrade/downgrade events).
"""

from abc import ABC, abstractmethod

from app.stocks.recommendations.entities import (
    AnalystRatingChanges,
    AnalystRecommendations,
)


class RecommendationProvider(ABC):
    """A gateway for a stock's analyst recommendation trends (and price-target consensus).

    The sell-side buy/hold/sell split, by month — the "what does the street
    think?" forward view, from a ratings vendor rather than the price feed — plus the
    current consensus price target riding on the returned run as best-effort enrichment.
    """

    @abstractmethod
    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        """Return recommendation trends for the (already-normalized) symbol,
        newest snapshot first, with the current ``price_targets`` block attached when the
        source serves one (``None`` otherwise — it never fails the call).

        Returns an ``is_empty`` run when no analyst covers the symbol — "no
        coverage" is not an error for this best-effort feature.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class RatingChangeProvider(ABC):
    """A gateway for a stock's individual sell-side rating actions (upgrades/downgrades).

    The discrete events behind the trend — one per firm action — newest first. A separate
    capability from the monthly aggregate, so it's a separate port.
    """

    @abstractmethod
    def get_rating_changes(self, symbol: str) -> AnalystRatingChanges:
        """Return the (already-normalized) symbol's rating actions, newest first.

        Returns an ``is_empty`` run when the source publishes none — "no coverage" is not
        an error for this best-effort feature.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError
