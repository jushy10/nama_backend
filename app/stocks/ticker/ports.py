from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from app.stocks.ticker.entities import OptionContract, ReportedEps


class OptionChainProvider(ABC):
    @abstractmethod
    def get_expirations(self, symbol: str) -> tuple[date, ...]:
        raise NotImplementedError

    @abstractmethod
    def get_chain(self, symbol: str, expiration: date) -> tuple[OptionContract, ...]:
        raise NotImplementedError


class EpsHistoryProvider(ABC):
    @abstractmethod
    def get_eps_history(self, symbol: str) -> tuple[ReportedEps, ...]:
        raise NotImplementedError
