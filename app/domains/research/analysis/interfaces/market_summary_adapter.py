from abc import ABC, abstractmethod

from app.domains.research.analysis.entities import MarketSummary
from app.domains.markets.boards.entities import MarketIndexPerformance


class MarketSummaryAdapter(ABC):
    @abstractmethod
    def analyze(self, indexes: list[MarketIndexPerformance]) -> MarketSummary:
        raise NotImplementedError
