from abc import ABC, abstractmethod
from datetime import date

from app.stocks.brief.entities import MarketBrief


class MarketBriefRepository(ABC):
    @abstractmethod
    def get(self, brief_date: date) -> MarketBrief | None:
        raise NotImplementedError

    @abstractmethod
    def latest(self) -> MarketBrief | None:
        raise NotImplementedError

    @abstractmethod
    def upsert(self, brief: MarketBrief) -> None:
        raise NotImplementedError
