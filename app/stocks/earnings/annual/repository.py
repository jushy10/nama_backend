from abc import ABC, abstractmethod
from typing import NamedTuple

from app.stocks.earnings.annual.entities import AnnualEarningsTimeline


class RefreshTarget(NamedTuple):
    symbol: str
    name: str | None


class AnnualEarningsRepository(ABC):
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
