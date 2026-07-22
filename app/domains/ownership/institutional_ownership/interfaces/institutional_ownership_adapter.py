from abc import ABC, abstractmethod
from app.domains.ownership.institutional_ownership.entities import InstitutionalOwnership


class InstitutionalOwnershipAdapter(ABC):
    @abstractmethod
    def get_institutional_ownership(self, symbol: str) -> InstitutionalOwnership:
        raise NotImplementedError
