from abc import ABC, abstractmethod
from app.stocks.company.news.entities import StockNews


class NewsAdapter(ABC):
    @abstractmethod
    def get_news(self, symbol: str) -> StockNews:
        raise NotImplementedError
