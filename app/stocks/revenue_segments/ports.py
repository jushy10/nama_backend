from abc import ABC, abstractmethod

from app.stocks.revenue_segments.entities import RevenueSegmentation


class RevenueSegmentsProvider(ABC):
    @abstractmethod
    def get_revenue_segments(self, symbol: str) -> RevenueSegmentation:
        raise NotImplementedError
