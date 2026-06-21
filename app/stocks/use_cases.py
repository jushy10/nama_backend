"""Application Business Rules: the GetStockInfo use case.

Orchestrates the flow: validate/normalize the symbol, then ask the injected
provider for the data. Depends only on the entity and the port — never on a
framework or a concrete provider.
"""

from app.stocks.entities import Stock
from app.stocks.ports import StockDataProvider


class GetStockInfo:
    """Use case: retrieve information about a single stock by its symbol."""

    def __init__(self, provider: StockDataProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> Stock:
        normalized = (symbol or "").strip().upper()
        if not normalized:
            raise ValueError("A stock symbol is required.")
        if not normalized.isalpha() or len(normalized) > 5:
            # Simple guard; real tickers are 1-5 letters (ignoring class suffixes).
            raise ValueError(f"'{symbol}' is not a valid stock symbol.")
        return self._provider.get_stock(normalized)
