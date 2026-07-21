from abc import ABC, abstractmethod
from app.stocks.market.yields.entities import YieldCurve


class YieldCurveAdapter(ABC):
    @abstractmethod
    def get_yield_curve(self) -> YieldCurve:
        raise NotImplementedError
