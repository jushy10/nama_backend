"""Application port: the abstraction the logo use case depends on.

The use case depends on this interface; the adapter layer provides the
Logo.dev-backed implementation. The core never imports the vendor.
"""

from abc import ABC, abstractmethod

from app.stocks.logo.entities import Logo


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
