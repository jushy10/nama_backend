from abc import ABC, abstractmethod

from app.stocks.ai.analysis.entities import StockScorecard
from app.stocks.company.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.company.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.company.recommendations.entities import AnalystRecommendations
from app.stocks.entities import Stock
from app.stocks.catalog.universe.entities import IndustryValuation


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
