from abc import ABC, abstractmethod
from datetime import date

from app.stocks.ai.brief.entities import MarketBrief, MarketBriefContext


class MarketBriefAdapter(ABC):
    @abstractmethod
    def generate(self, context: MarketBriefContext, brief_date: date) -> MarketBrief:
        raise NotImplementedError
