from abc import ABC, abstractmethod
from app.domains.shared.entities import Stock


class StockDataAdapter(ABC):
    @abstractmethod
    def get_stock(self, symbol: str) -> Stock:
        raise NotImplementedError
