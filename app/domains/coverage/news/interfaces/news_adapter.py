from abc import ABC, abstractmethod
from app.domains.coverage.news.entities import StockNews


class NewsAdapter(ABC):
    @abstractmethod
    def get_news(self, symbol: str) -> StockNews:
        raise NotImplementedError
