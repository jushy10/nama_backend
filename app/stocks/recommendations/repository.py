from abc import ABC, abstractmethod
from typing import NamedTuple

from app.stocks.recommendations.entities import (
    AnalystRatingChanges,
    AnalystRecommendations,
)


class RefreshTarget(NamedTuple):
    symbol: str
    name: str | None


class RecommendationsRepository(ABC):
    @abstractmethod
    def get(self, symbol: str) -> AnalystRecommendations | None:
        raise NotImplementedError

    @abstractmethod
    def upsert(
        self, symbol: str, name: str | None, recommendations: AnalystRecommendations
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        raise NotImplementedError


class RatingChangesRepository(ABC):
    @abstractmethod
    def get(self, symbol: str) -> AnalystRatingChanges | None:
        raise NotImplementedError

    @abstractmethod
    def upsert(
        self, symbol: str, name: str | None, rating_changes: AnalystRatingChanges
    ) -> None:
        raise NotImplementedError
