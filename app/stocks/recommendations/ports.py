from abc import ABC, abstractmethod

from app.stocks.recommendations.entities import (
    AnalystRatingChanges,
    AnalystRecommendations,
)


class RecommendationProvider(ABC):
    @abstractmethod
    def get_recommendations(self, symbol: str) -> AnalystRecommendations:
        raise NotImplementedError


class RatingChangeProvider(ABC):
    @abstractmethod
    def get_rating_changes(self, symbol: str) -> AnalystRatingChanges:
        raise NotImplementedError
