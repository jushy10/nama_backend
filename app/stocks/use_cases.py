"""Application Business Rules: the stock use cases.

Orchestrate the flow: validate/normalize the symbol, then ask the injected
provider for the data. Depend only on the entity and the port — never on a
framework or a concrete provider.
"""

from datetime import datetime

from app.stocks.entities import CandleSeries, Logo, Stock, Timeframe
from app.stocks.ports import CandleProvider, LogoProvider, StockDataProvider


def _normalize_symbol(symbol: str) -> str:
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("A stock symbol is required.")
    if not normalized.isalpha() or len(normalized) > 5:
        # Simple guard; real tickers are 1-5 letters (ignoring class suffixes).
        raise ValueError(f"'{symbol}' is not a valid stock symbol.")
    return normalized


class GetStockInfo:
    """Use case: retrieve information about a single stock by its symbol."""

    def __init__(self, provider: StockDataProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> Stock:
        return self._provider.get_stock(_normalize_symbol(symbol))


class GetStockLogo:
    """Use case: retrieve the company logo image for a stock symbol."""

    def __init__(self, provider: LogoProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> Logo:
        return self._provider.get_logo(_normalize_symbol(symbol))


class GetStockCandles:
    """Use case: retrieve historical OHLC candles for charting."""

    def __init__(self, provider: CandleProvider) -> None:
        self._provider = provider

    def execute(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> CandleSeries:
        if start is not None and end is not None and start >= end:
            raise ValueError("'start' must be earlier than 'end'.")
        return self._provider.get_candles(
            _normalize_symbol(symbol), timeframe, start=start, end=end
        )
