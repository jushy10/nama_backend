from abc import ABC, abstractmethod

from app.stocks.market.boards.entities import MarketIndexPerformance, SectorPerformance


class SectorPerformanceProvider(ABC):
    @abstractmethod
    def get_sector_performance(self) -> list[SectorPerformance]:
        raise NotImplementedError


class MarketOverviewProvider(ABC):
    @abstractmethod
    def get_market_overview(self) -> list[MarketIndexPerformance]:
        raise NotImplementedError
