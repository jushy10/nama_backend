from abc import ABC, abstractmethod
from app.domains.coverage.recommendations.entities import AnalystRatingChanges


class RatingChangesRepositoryAdapter(ABC):
    @abstractmethod
    def get(self, symbol: str) -> AnalystRatingChanges | None:
        raise NotImplementedError

    @abstractmethod
    def upsert(
        self, symbol: str, name: str | None, rating_changes: AnalystRatingChanges
    ) -> None:
        raise NotImplementedError
