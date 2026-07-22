from abc import ABC, abstractmethod
from app.domains.pricing.ticker.interfaces.types import StoredTickerFacts


class TickerRepositoryAdapter(ABC):
    @abstractmethod
    def get_facts(self, symbol: str) -> StoredTickerFacts:
        raise NotImplementedError

    @abstractmethod
    def save_name(self, symbol: str, name: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def save_exchange(self, symbol: str, exchange: str) -> None:
        raise NotImplementedError
