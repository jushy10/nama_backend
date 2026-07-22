from abc import ABC, abstractmethod

from app.domains.research.analysis.entities import FundamentalsAnalysis
from app.domains.pricing.ticker.entities import PeHistoryStats
from app.domains.shared.entities import Stock
from app.domains.listings.universe.entities import IndustryValuation


class FundamentalsAnalysisAdapter(ABC):
    @abstractmethod
    def analyze(
        self,
        stock: Stock,
        industry_valuation: IndustryValuation | None = None,
        pe_history: PeHistoryStats | None = None,
    ) -> FundamentalsAnalysis:
        raise NotImplementedError
