from __future__ import annotations

from datetime import datetime

from app.stocks.company.charts.interfaces import CandleAdapter
from app.stocks.entities import (
    AllTimeHigh,
    CandleSeries,
    Quote,
    Stock,
    StockPerformance,
    Timeframe,
    is_canadian,  # re-exported: the market-identity rule lives in the shared kernel
)
from app.stocks.interfaces import (
    AllTimeHighAdapter,
    StockDataAdapter,
    StockPerformanceAdapter,
    StockQuoteAdapter,
)

__all__ = ["PriceAdapterImpl", "is_canadian"]


class PriceAdapterImpl(
    StockDataAdapter,
    StockQuoteAdapter,
    StockPerformanceAdapter,
    AllTimeHighAdapter,
    CandleAdapter,
):
    def __init__(self, *, us, ca) -> None:
        self._us = us
        self._ca = ca

    def _for(self, symbol: str):
        return self._ca if is_canadian(symbol) else self._us

    def get_stock(self, symbol: str) -> Stock:
        return self._for(symbol).get_stock(symbol)

    def get_quote(self, symbol: str) -> Quote:
        return self._for(symbol).get_quote(symbol)

    def get_performance(self, symbol: str) -> StockPerformance:
        return self._for(symbol).get_performance(symbol)

    def get_all_time_high(self, symbol: str) -> AllTimeHigh:
        return self._for(symbol).get_all_time_high(symbol)

    def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> CandleSeries:
        return self._for(symbol).get_candles(symbol, timeframe, start=start, end=end)
