from abc import ABC, abstractmethod
from app.domains.listings.fundamentals.entities import Fundamentals


class FundamentalsAdapter(ABC):
    @abstractmethod
    def get_fundamentals(self, symbol: str) -> Fundamentals:
        raise NotImplementedError
