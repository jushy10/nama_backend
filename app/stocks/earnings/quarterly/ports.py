from abc import ABC, abstractmethod

from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline


class QuarterlyEarningsProvider(ABC):
    @abstractmethod
    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        raise NotImplementedError
