from abc import ABC, abstractmethod

from app.stocks.ai.analysis.entities import MarketSummary
from app.stocks.market.boards.entities import MarketIndexPerformance


class MarketSummaryProvider(ABC):
    @abstractmethod
    def analyze(self, indexes: list[MarketIndexPerformance]) -> MarketSummary:
        raise NotImplementedError
