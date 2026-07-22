from abc import ABC, abstractmethod
from app.domains.coverage.recommendations.entities import AnalystRecommendations
from app.domains.coverage.recommendations.interfaces.types import RefreshTarget


class RecommendationsRepositoryAdapter(ABC):
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
