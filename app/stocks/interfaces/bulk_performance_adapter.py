from abc import ABC, abstractmethod
from collections.abc import Sequence
from app.stocks.entities import StockPerformance


class BulkPerformanceAdapter(ABC):
    @abstractmethod
    def get_bulk_performance(
        self, symbols: Sequence[str]
    ) -> dict[str, StockPerformance]:
        raise NotImplementedError
