from abc import ABC, abstractmethod
from app.domains.markets.boards.entities import MarketIndexPerformance


class MarketOverviewAdapter(ABC):
    @abstractmethod
    def get_market_overview(self) -> list[MarketIndexPerformance]:
        raise NotImplementedError
