from abc import ABC, abstractmethod
from app.stocks.entities import Quote


class StockQuoteAdapter(ABC):
    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError
