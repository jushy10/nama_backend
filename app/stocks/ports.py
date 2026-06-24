"""Application port: the abstraction the use case depends on.

This is the Dependency Inversion that makes the slice clean: the use case
depends on this interface, and the adapter layer provides the Alpaca-backed
implementation. The core never imports Alpaca; Alpaca imports the core.
"""

from abc import ABC, abstractmethod

from app.stocks.entities import Logo, Stock, StockFundamentals, StockPerformance


class StockDataProvider(ABC):
    """A gateway for retrieving stock data from some external source."""

    @abstractmethod
    def get_stock(self, symbol: str) -> Stock:
        """Return a Stock for the given (already-normalized) symbol.

        Raises:
            StockNotFound: the symbol does not exist / has no data.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class StockPerformanceProvider(ABC):
    """A gateway for a stock's trailing price-return over standard windows.

    Separate from StockDataProvider: performance is derived from price history
    rather than the live snapshot, and the endpoint treats it as best-effort
    enrichment, so a failure here must not sink the price response.
    """

    @abstractmethod
    def get_performance(self, symbol: str) -> StockPerformance:
        """Return trailing-window performance for the (normalized) symbol.

        Raises:
            StockNotFound: the symbol has no price history.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class StockFundamentalsProvider(ABC):
    """A gateway for company fundamentals (market cap, dividend).

    These come from a fundamentals vendor, not the price feed — market data
    APIs don't expose shares outstanding or dividends. Best-effort enrichment.
    """

    @abstractmethod
    def get_fundamentals(self, symbol: str) -> StockFundamentals:
        """Return fundamentals for the (already-normalized) symbol.

        Raises:
            StockNotFound: the symbol is not covered by the source.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError


class LogoProvider(ABC):
    """A gateway for retrieving a company's logo image.

    Separate from StockDataProvider because logos and market data come from
    different vendors — Alpaca's logo endpoint is paywalled, so logos are
    sourced elsewhere without disturbing the price-data adapter.
    """

    @abstractmethod
    def get_logo(self, symbol: str) -> Logo:
        """Return the logo for the given (already-normalized) symbol.

        Raises:
            StockNotFound: no logo is available for the symbol.
            StockDataUnavailable: the upstream source failed.
        """
        raise NotImplementedError
