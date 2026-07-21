from abc import ABC, abstractmethod

from app.stocks.catalog.fundamentals.entities import Fundamentals


class FundamentalsProvider(ABC):
    @abstractmethod
    def get_fundamentals(self, symbol: str) -> Fundamentals:
        raise NotImplementedError
