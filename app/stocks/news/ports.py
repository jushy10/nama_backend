from abc import ABC, abstractmethod

from app.stocks.news.entities import StockNews


class NewsProvider(ABC):
    @abstractmethod
    def get_news(self, symbol: str) -> StockNews:
        raise NotImplementedError
