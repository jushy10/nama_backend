from abc import ABC, abstractmethod

from app.stocks.ai.analysis.entities import RatingsAnalysis
from app.stocks.company.recommendations.entities import AnalystRecommendations, FirmRating


class RatingsAnalysisProvider(ABC):
    @abstractmethod
    def analyze(
        self,
        symbol: str,
        recommendations: AnalystRecommendations | None = None,
        top_firms: tuple[FirmRating, ...] = (),
    ) -> RatingsAnalysis:
        raise NotImplementedError
