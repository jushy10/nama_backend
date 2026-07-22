from abc import ABC, abstractmethod

from app.domains.research.analysis.entities import StockScorecard
from app.domains.financials.earnings.annual.entities import AnnualEarningsTimeline
from app.domains.financials.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.domains.coverage.recommendations.entities import AnalystRecommendations
from app.domains.shared.entities import Stock
from app.domains.listings.universe.entities import IndustryValuation


class StockScorecardAdapter(ABC):
    @abstractmethod
    def analyze(
        self,
        stock: Stock,
        quarterly: QuarterlyEarningsTimeline | None = None,
        annual: AnnualEarningsTimeline | None = None,
        recommendations: AnalystRecommendations | None = None,
        industry_valuation: IndustryValuation | None = None,
    ) -> StockScorecard:
        raise NotImplementedError
