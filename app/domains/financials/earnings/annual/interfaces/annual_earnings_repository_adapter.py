from abc import ABC, abstractmethod
from app.domains.financials.earnings.annual.entities import AnnualEarningsTimeline
from app.domains.financials.earnings.annual.interfaces.types import RefreshTarget


class AnnualEarningsRepositoryAdapter(ABC):
    @abstractmethod
    def get(self, symbol: str) -> AnnualEarningsTimeline | None:
        raise NotImplementedError

    @abstractmethod
    def upsert(
        self, symbol: str, name: str | None, timeline: AnnualEarningsTimeline
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        raise NotImplementedError
