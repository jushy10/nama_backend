"""Application port: the abstraction the use case depends on.

This is the Dependency Inversion that makes the slice clean: the use case
depends on this interface, and the adapter layer provides the Alpaca-backed
implementation. The core never imports Alpaca; Alpaca imports the core.
"""

from abc import ABC, abstractmethod

from app.stocks.entities import Stock


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
