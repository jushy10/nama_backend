from abc import ABC, abstractmethod
from app.domains.financials.revenue_segments.entities import RevenueSegmentation


class RevenueSegmentsAdapter(ABC):
    @abstractmethod
    def get_revenue_segments(self, symbol: str) -> RevenueSegmentation:
        raise NotImplementedError
