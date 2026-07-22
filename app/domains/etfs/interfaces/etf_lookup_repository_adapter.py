from abc import ABC, abstractmethod
from app.domains.etfs.entities import EtfProfile, EtfSearchResult


class EtfLookupRepositoryAdapter(ABC):
    @abstractmethod
    def is_etf(self, ticker: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get(self, ticker: str) -> EtfSearchResult | None:
        raise NotImplementedError

    @abstractmethod
    def get_stored_profile(self, ticker: str) -> EtfProfile:
        raise NotImplementedError
