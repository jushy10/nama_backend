from abc import ABC, abstractmethod
from app.domains.markets.boards.entities import SectorPerformance


class SectorPerformanceAdapter(ABC):
    @abstractmethod
    def get_sector_performance(self) -> list[SectorPerformance]:
        raise NotImplementedError
