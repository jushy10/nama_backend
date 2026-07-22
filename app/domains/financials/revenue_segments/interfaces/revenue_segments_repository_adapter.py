from abc import ABC, abstractmethod
from app.domains.financials.revenue_segments.entities import RevenueSegmentation
from app.domains.financials.revenue_segments.interfaces.types import RefreshTarget


class RevenueSegmentsRepositoryAdapter(ABC):
    @abstractmethod
    def get(self, symbol: str) -> RevenueSegmentation | None:
        raise NotImplementedError

    @abstractmethod
    def upsert(
        self, symbol: str, name: str | None, segmentation: RevenueSegmentation
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        raise NotImplementedError
