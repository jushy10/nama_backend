from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.stocks.catalog.universe.entities import (
    CompanyClassification,
    ScreenedStock,
    ScreenIntent,
)


class StockScreener(ABC):
    @abstractmethod
    def screen(
        self, *, min_market_cap: float, region: str = "us"
    ) -> tuple[ScreenedStock, ...]:
        raise NotImplementedError


class CompanyClassificationProvider(ABC):
    @abstractmethod
    def get_classification(self, symbol: str) -> CompanyClassification:
        raise NotImplementedError


class ScreenerQueryTranslator(ABC):
    @abstractmethod
    def translate(
        self,
        query: str,
        *,
        sectors: Sequence[str],
        industries: Sequence[str],
    ) -> ScreenIntent:
        raise NotImplementedError
