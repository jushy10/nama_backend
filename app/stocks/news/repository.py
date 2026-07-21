from abc import ABC, abstractmethod
from typing import NamedTuple

from app.stocks.news.entities import StockNews


class RefreshTarget(NamedTuple):
    symbol: str
    name: str | None


class NewsRepository(ABC):
    @abstractmethod
    def get(self, symbol: str) -> StockNews | None:
        raise NotImplementedError

    @abstractmethod
    def upsert(self, symbol: str, name: str | None, news: StockNews) -> None:
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        raise NotImplementedError
