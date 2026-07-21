from abc import ABC, abstractmethod
from typing import NamedTuple

from app.stocks.insider_transactions.entities import InsiderActivity


class RefreshTarget(NamedTuple):
    symbol: str
    name: str | None


class InsiderTransactionsRepository(ABC):
    @abstractmethod
    def get(self, symbol: str) -> InsiderActivity | None:
        raise NotImplementedError

    @abstractmethod
    def upsert(self, symbol: str, name: str | None, activity: InsiderActivity) -> None:
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        raise NotImplementedError
