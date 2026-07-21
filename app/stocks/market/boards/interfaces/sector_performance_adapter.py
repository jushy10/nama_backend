from abc import ABC, abstractmethod
from app.stocks.market.boards.entities import SectorPerformance


class SectorPerformanceAdapter(ABC):
    @abstractmethod
    def get_sector_performance(self) -> list[SectorPerformance]:
        raise NotImplementedError
