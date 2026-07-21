from abc import ABC, abstractmethod
from app.stocks.company.earnings.annual.entities import AnnualEarningsTimeline


class AnnualEarningsAdapter(ABC):
    @abstractmethod
    def get_annual_earnings(self, symbol: str) -> AnnualEarningsTimeline:
        raise NotImplementedError
