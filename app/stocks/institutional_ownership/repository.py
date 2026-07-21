from abc import ABC, abstractmethod
from typing import NamedTuple

from app.stocks.institutional_ownership.entities import InstitutionalOwnership


class RefreshTarget(NamedTuple):
    symbol: str
    name: str | None


class InstitutionalOwnershipRepository(ABC):
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
