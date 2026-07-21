from abc import ABC, abstractmethod
from app.stocks.company.news.entities import StockNews
from app.stocks.company.news.interfaces.types import RefreshTarget


class NewsRepositoryAdapter(ABC):
    @abstractmethod
    def get(self, symbol: str) -> StockNews | None:
        raise NotImplementedError

    @abstractmethod
    def upsert(self, symbol: str, name: str | None, news: StockNews) -> None:
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        raise NotImplementedError
