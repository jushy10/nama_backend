from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.stocks.catalog.etfs.entities import (
    EtfCategories,
    EtfProfile,
    EtfSearchCriteria,
    EtfSearchPage,
    EtfSearchResult,
    ScreenedEtf,
)


@dataclass(frozen=True)
class EtfSyncCounts:
    added: int
    updated: int


class EtfRepository(ABC):
    @abstractmethod
    def upsert_screen(self, etfs: tuple[ScreenedEtf, ...]) -> EtfSyncCounts:
        raise NotImplementedError

    @abstractmethod
    def profile_refresh_targets(self, limit: int | None) -> tuple[str, ...]:
        raise NotImplementedError

    @abstractmethod
    def upsert_profile(self, ticker: str, profile: EtfProfile) -> None:
        raise NotImplementedError


class EtfSearchRepository(ABC):
    @abstractmethod
    def search(self, criteria: EtfSearchCriteria) -> EtfSearchPage:
        raise NotImplementedError

    @abstractmethod
    def categories(self) -> EtfCategories:
        raise NotImplementedError


class EtfLookupRepository(ABC):
    @abstractmethod
    def is_etf(self, ticker: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get(self, ticker: str) -> EtfSearchResult | None:
        raise NotImplementedError

    @abstractmethod
    def get_stored_profile(self, ticker: str) -> EtfProfile:
        raise NotImplementedError
