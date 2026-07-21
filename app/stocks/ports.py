from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.stocks.entities import (
    AllTimeHigh,
    AnalystEstimates,
    Quote,
    Stock,
    StockPerformance,
)


class StockDataProvider(ABC):
    @abstractmethod
    def get_stock(self, symbol: str) -> Stock:
        raise NotImplementedError


class StockQuoteProvider(ABC):
    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError


class BulkQuoteProvider(ABC):
    @abstractmethod
    def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        raise NotImplementedError


class StockPerformanceProvider(ABC):
    @abstractmethod
    def get_performance(self, symbol: str) -> StockPerformance:
        raise NotImplementedError


class BulkPerformanceProvider(ABC):
    @abstractmethod
    def get_bulk_performance(
        self, symbols: Sequence[str]
    ) -> dict[str, StockPerformance]:
        raise NotImplementedError


class AllTimeHighProvider(ABC):
    @abstractmethod
    def get_all_time_high(self, symbol: str) -> AllTimeHigh:
        raise NotImplementedError


class AnalystEstimatesProvider(ABC):
    @abstractmethod
    def get_estimates(self, symbol: str) -> AnalystEstimates:
        raise NotImplementedError
