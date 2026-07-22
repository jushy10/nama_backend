from abc import ABC, abstractmethod
from datetime import date
from app.domains.pricing.ticker.entities import OptionContract


class OptionChainAdapter(ABC):
    @abstractmethod
    def get_expirations(self, symbol: str) -> tuple[date, ...]:
        raise NotImplementedError

    @abstractmethod
    def get_chain(self, symbol: str, expiration: date) -> tuple[OptionContract, ...]:
        raise NotImplementedError
