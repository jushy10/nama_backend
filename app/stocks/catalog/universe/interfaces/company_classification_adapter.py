from abc import ABC, abstractmethod
from app.stocks.catalog.universe.entities import CompanyClassification


class CompanyClassificationAdapter(ABC):
    @abstractmethod
    def get_classification(self, symbol: str) -> CompanyClassification:
        raise NotImplementedError
