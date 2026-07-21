from abc import ABC, abstractmethod
from app.stocks.entities import AllTimeHigh


class AllTimeHighAdapter(ABC):
    @abstractmethod
    def get_all_time_high(self, symbol: str) -> AllTimeHigh:
        raise NotImplementedError
