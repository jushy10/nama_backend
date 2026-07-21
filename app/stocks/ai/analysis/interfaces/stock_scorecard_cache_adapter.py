from abc import ABC, abstractmethod

from app.stocks.ai.analysis.entities import StockScorecard


class StockScorecardCacheAdapter(ABC):
    @abstractmethod
    def get(self, symbol: str) -> StockScorecard | None:
        raise NotImplementedError

    @abstractmethod
    def put(self, scorecard: StockScorecard) -> None:
        raise NotImplementedError
