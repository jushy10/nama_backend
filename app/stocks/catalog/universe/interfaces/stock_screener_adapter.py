from abc import ABC, abstractmethod
from app.stocks.catalog.universe.entities import ScreenedStock


class StockScreenerAdapter(ABC):
    @abstractmethod
    def screen(
        self, *, min_market_cap: float, region: str = "us"
    ) -> tuple[ScreenedStock, ...]:
        raise NotImplementedError
