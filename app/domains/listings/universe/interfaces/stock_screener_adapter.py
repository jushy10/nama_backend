from abc import ABC, abstractmethod
from app.domains.listings.universe.entities import ScreenedStock


class StockScreenerAdapter(ABC):
    @abstractmethod
    def screen(
        self, *, min_market_cap: float, region: str = "us"
    ) -> tuple[ScreenedStock, ...]:
        raise NotImplementedError
