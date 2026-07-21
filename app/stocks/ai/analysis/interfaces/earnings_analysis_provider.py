from abc import ABC, abstractmethod

from app.stocks.ai.analysis.entities import EarningsAnalysis
from app.stocks.company.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.company.earnings.quarterly.entities import QuarterlyEarningsTimeline


class EarningsAnalysisProvider(ABC):
    @abstractmethod
    def analyze(
        self,
        symbol: str,
        quarterly: QuarterlyEarningsTimeline | None = None,
        annual: AnnualEarningsTimeline | None = None,
    ) -> EarningsAnalysis:
        raise NotImplementedError
