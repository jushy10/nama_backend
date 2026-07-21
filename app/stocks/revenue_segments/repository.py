from abc import ABC, abstractmethod
from typing import NamedTuple

from app.stocks.revenue_segments.entities import RevenueSegmentation


class RefreshTarget(NamedTuple):
    symbol: str
    name: str | None


class RevenueSegmentsRepository(ABC):
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
