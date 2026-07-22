from abc import ABC, abstractmethod

from app.domains.research.analysis.entities import RatingsAnalysis
from app.domains.coverage.recommendations.entities import AnalystRecommendations, FirmRating


class RatingsAnalysisAdapter(ABC):
    @abstractmethod
    def analyze(
        self,
        symbol: str,
        recommendations: AnalystRecommendations | None = None,
        top_firms: tuple[FirmRating, ...] = (),
    ) -> RatingsAnalysis:
        raise NotImplementedError
