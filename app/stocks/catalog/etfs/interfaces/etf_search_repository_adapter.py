from abc import ABC, abstractmethod
from app.stocks.catalog.etfs.entities import EtfCategories, EtfSearchCriteria, EtfSearchPage


class EtfSearchRepositoryAdapter(ABC):
    @abstractmethod
    def search(self, criteria: EtfSearchCriteria) -> EtfSearchPage:
        raise NotImplementedError

    @abstractmethod
    def categories(self) -> EtfCategories:
        raise NotImplementedError
