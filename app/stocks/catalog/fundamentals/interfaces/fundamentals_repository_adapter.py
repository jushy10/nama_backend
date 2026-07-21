from abc import ABC, abstractmethod
from app.stocks.catalog.fundamentals.entities import Fundamentals
from app.stocks.catalog.fundamentals.interfaces.types import RefreshTarget


class FundamentalsRepositoryAdapter(ABC):
    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        raise NotImplementedError

    @abstractmethod
    def upsert(self, symbol: str, name: str | None, fundamentals: Fundamentals) -> None:
        raise NotImplementedError
