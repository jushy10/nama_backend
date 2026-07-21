from abc import ABC, abstractmethod
from app.stocks.company.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.company.earnings.quarterly.interfaces.types import RefreshTarget


class QuarterlyEarningsRepositoryAdapter(ABC):
    @abstractmethod
    def get(self, symbol: str) -> QuarterlyEarningsTimeline | None:
        raise NotImplementedError

    @abstractmethod
    def upsert(
        self, symbol: str, name: str | None, timeline: QuarterlyEarningsTimeline
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        raise NotImplementedError
