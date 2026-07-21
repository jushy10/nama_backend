from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from app.stocks.analysis.entities import (
    EarningsAnalysis,
    FundamentalsAnalysis,
    InvestmentAnalysis,
    MarketSummary,
    RatingsAnalysis,
    SectorAnalysis,
    SectorContext,
    StockScorecard,
)
from app.stocks.earnings.annual.entities import AnnualEarningsTimeline
from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.entities import Stock
from app.stocks.market.entities import MarketIndexPerformance
from app.stocks.recommendations.entities import AnalystRecommendations, FirmRating
from app.stocks.ticker.entities import PeHistoryStats
from app.stocks.universe.entities import IndustryValuation


class StockScorecardProvider(ABC):
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


class StockScorecardCache(ABC):
    @abstractmethod
    def get(self, symbol: str) -> StockScorecard | None:
        raise NotImplementedError

    @abstractmethod
    def put(self, scorecard: StockScorecard) -> None:
        raise NotImplementedError


class InvestmentAnalysisCache(ABC):
    @abstractmethod
    def get(self, symbol: str) -> InvestmentAnalysis | None:
        raise NotImplementedError

    @abstractmethod
    def put(self, analysis: InvestmentAnalysis) -> None:
        raise NotImplementedError


T = TypeVar("T")


class AiAnalysisCache(ABC, Generic[T]):
    @abstractmethod
    def get(self, key: str) -> T | None:
        raise NotImplementedError

    @abstractmethod
    def put(self, key: str, analysis: T) -> None:
        raise NotImplementedError


class SectorAnalysisProvider(ABC):
    @abstractmethod
    def analyze(self, contexts: list[SectorContext]) -> SectorAnalysis:
        raise NotImplementedError


class MarketSummaryProvider(ABC):
    @abstractmethod
    def analyze(self, indexes: list[MarketIndexPerformance]) -> MarketSummary:
        raise NotImplementedError


class EarningsAnalysisProvider(ABC):
    @abstractmethod
    def analyze(
        self,
        symbol: str,
        quarterly: QuarterlyEarningsTimeline | None = None,
        annual: AnnualEarningsTimeline | None = None,
    ) -> EarningsAnalysis:
        raise NotImplementedError


class RatingsAnalysisProvider(ABC):
    @abstractmethod
    def analyze(
        self,
        symbol: str,
        recommendations: AnalystRecommendations | None = None,
        top_firms: tuple[FirmRating, ...] = (),
    ) -> RatingsAnalysis:
        raise NotImplementedError


class FundamentalsAnalysisProvider(ABC):
    @abstractmethod
    def analyze(
        self,
        stock: Stock,
        industry_valuation: IndustryValuation | None = None,
        pe_history: PeHistoryStats | None = None,
    ) -> FundamentalsAnalysis:
        raise NotImplementedError
