"""Domain-level errors for the stocks feature.

Expressed in business terms, independent of HTTP or Alpaca. Outer layers
translate them (e.g. StockNotFound -> HTTP 404).
"""


class StockNotFound(Exception):
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__(f"No stock data found for symbol '{symbol}'.")


class StockDataUnavailable(Exception):
    """The upstream data source failed for a reason other than 'not found'."""

    def __init__(self, symbol: str, reason: str) -> None:
        self.symbol = symbol
        self.reason = reason
        super().__init__(f"Stock data for '{symbol}' is unavailable: {reason}")
