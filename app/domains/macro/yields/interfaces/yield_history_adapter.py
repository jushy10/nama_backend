from abc import ABC, abstractmethod
from app.domains.macro.yields.entities import YieldHistory


class YieldHistoryAdapter(ABC):
    @abstractmethod
    def get_yield_history(self, lookback_days: int) -> YieldHistory:
        raise NotImplementedError
