from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from app.stocks.options.entities import ExpiryChain


class OptionsChainProvider(ABC):
    @abstractmethod
    def get_expirations(self, symbol: str) -> tuple[date, ...]:
        raise NotImplementedError

    @abstractmethod
    def get_chain(self, symbol: str, expiration: date) -> ExpiryChain:
        raise NotImplementedError
