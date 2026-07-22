from abc import ABC, abstractmethod
from app.domains.financials.earnings.quarterly.entities import QuarterlyEarningsTimeline


class QuarterlyEarningsAdapter(ABC):
    @abstractmethod
    def get_quarterly_earnings(self, symbol: str) -> QuarterlyEarningsTimeline:
        raise NotImplementedError
