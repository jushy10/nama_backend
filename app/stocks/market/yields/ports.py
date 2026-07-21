from abc import ABC, abstractmethod

from app.stocks.market.yields.entities import YieldCurve, YieldHistory


class YieldCurveProvider(ABC):
    @abstractmethod
    def get_yield_curve(self) -> YieldCurve:
        raise NotImplementedError


class YieldHistoryProvider(ABC):
    @abstractmethod
    def get_yield_history(self, lookback_days: int) -> YieldHistory:
        raise NotImplementedError
