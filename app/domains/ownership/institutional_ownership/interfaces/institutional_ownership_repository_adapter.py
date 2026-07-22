from abc import ABC, abstractmethod
from app.domains.ownership.institutional_ownership.entities import InstitutionalOwnership
from app.domains.ownership.institutional_ownership.interfaces.types import RefreshTarget


class InstitutionalOwnershipRepositoryAdapter(ABC):
    @abstractmethod
    def get(self, symbol: str) -> InstitutionalOwnership | None:
        raise NotImplementedError

    @abstractmethod
    def upsert(
        self, symbol: str, name: str | None, ownership: InstitutionalOwnership
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        raise NotImplementedError
