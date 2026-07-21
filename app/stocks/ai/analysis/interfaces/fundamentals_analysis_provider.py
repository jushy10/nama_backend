from abc import ABC, abstractmethod

from app.stocks.ai.analysis.entities import FundamentalsAnalysis
from app.stocks.company.ticker.entities import PeHistoryStats
from app.stocks.entities import Stock
from app.stocks.catalog.universe.entities import IndustryValuation


class FundamentalsAnalysisProvider(ABC):
    @abstractmethod
    def analyze(
        self,
        stock: Stock,
        industry_valuation: IndustryValuation | None = None,
        pe_history: PeHistoryStats | None = None,
    ) -> FundamentalsAnalysis:
        raise NotImplementedError
