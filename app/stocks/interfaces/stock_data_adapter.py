from abc import ABC, abstractmethod
from app.stocks.entities import Stock


class StockDataAdapter(ABC):
    @abstractmethod
    def get_stock(self, symbol: str) -> Stock:
        raise NotImplementedError
