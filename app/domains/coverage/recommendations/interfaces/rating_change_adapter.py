from abc import ABC, abstractmethod
from app.domains.coverage.recommendations.entities import AnalystRatingChanges


class RatingChangeAdapter(ABC):
    @abstractmethod
    def get_rating_changes(self, symbol: str) -> AnalystRatingChanges:
        raise NotImplementedError
