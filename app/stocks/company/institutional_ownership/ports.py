from abc import ABC, abstractmethod

from app.stocks.company.institutional_ownership.entities import InstitutionalOwnership


class InstitutionalOwnershipProvider(ABC):
    @abstractmethod
    def get_institutional_ownership(self, symbol: str) -> InstitutionalOwnership:
        raise NotImplementedError
