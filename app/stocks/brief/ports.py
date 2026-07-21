from abc import ABC, abstractmethod
from datetime import date

from app.stocks.brief.entities import MarketBrief, MarketBriefContext


class MarketBriefProvider(ABC):
    @abstractmethod
    def generate(self, context: MarketBriefContext, brief_date: date) -> MarketBrief:
        raise NotImplementedError
