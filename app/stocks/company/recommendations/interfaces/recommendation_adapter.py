from abc import ABC, abstractmethod
from app.stocks.company.recommendations.entities import AnalystRecommendations


class RecommendationAdapter(ABC):
    @abstractmethod
    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        raise NotImplementedError
