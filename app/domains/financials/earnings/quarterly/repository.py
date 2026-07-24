from abc import ABC, abstractmethod
from typing import NamedTuple

from app.domains.financials.earnings.quarterly.entities import QuarterlyEarningsTimeline


class RefreshTarget(NamedTuple):
    symbol: str
    name: str | None


class QuarterlyEarningsRepository(ABC):
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
