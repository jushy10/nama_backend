from abc import ABC, abstractmethod
from typing import NamedTuple

from app.stocks.catalog.fundamentals.entities import Fundamentals


class RefreshTarget(NamedTuple):
    symbol: str
    name: str | None


class FundamentalsRepository(ABC):
    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        raise NotImplementedError

    @abstractmethod
    def upsert(self, symbol: str, name: str | None, fundamentals: Fundamentals) -> None:
        raise NotImplementedError
