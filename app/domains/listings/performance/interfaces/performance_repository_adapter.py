from abc import ABC, abstractmethod
from collections.abc import Mapping
from app.domains.shared.entities import StockPerformance


class PerformanceRepositoryAdapter(ABC):
    @abstractmethod
    def refresh_targets(self, limit: int | None) -> tuple[str, ...]:
        raise NotImplementedError

    @abstractmethod
    def set_performance(
        self, performance_by_ticker: Mapping[str, StockPerformance]
    ) -> int:
        raise NotImplementedError
