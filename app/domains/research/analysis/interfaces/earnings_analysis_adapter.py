from abc import ABC, abstractmethod

from app.domains.research.analysis.entities import EarningsAnalysis
from app.domains.financials.earnings.annual.entities import AnnualEarningsTimeline
from app.domains.financials.earnings.quarterly.entities import QuarterlyEarningsTimeline


class EarningsAnalysisAdapter(ABC):
    @abstractmethod
    def analyze(
        self,
        symbol: str,
        quarterly: QuarterlyEarningsTimeline | None = None,
        annual: AnnualEarningsTimeline | None = None,
    ) -> EarningsAnalysis:
        raise NotImplementedError
