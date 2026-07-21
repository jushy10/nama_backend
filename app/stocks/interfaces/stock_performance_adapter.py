from abc import ABC, abstractmethod
from app.stocks.entities import StockPerformance


class StockPerformanceAdapter(ABC):
    @abstractmethod
    def get_performance(self, symbol: str) -> StockPerformance:
        raise NotImplementedError
