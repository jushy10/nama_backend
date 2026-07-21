from abc import ABC, abstractmethod
from app.stocks.company.insider_transactions.entities import InsiderActivity
from app.stocks.company.insider_transactions.interfaces.types import RefreshTarget


class InsiderTransactionsRepositoryAdapter(ABC):
    @abstractmethod
    def get(self, symbol: str) -> InsiderActivity | None:
        raise NotImplementedError

    @abstractmethod
    def upsert(self, symbol: str, name: str | None, activity: InsiderActivity) -> None:
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        raise NotImplementedError
