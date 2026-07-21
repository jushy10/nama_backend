from abc import ABC, abstractmethod
from app.stocks.catalog.etfs.entities import EtfProfile, ScreenedEtf
from app.stocks.catalog.etfs.interfaces.types import EtfSyncCounts


class EtfRepositoryAdapter(ABC):
    @abstractmethod
    def upsert_screen(self, etfs: tuple[ScreenedEtf, ...]) -> EtfSyncCounts:
        raise NotImplementedError

    @abstractmethod
    def profile_refresh_targets(self, limit: int | None) -> tuple[str, ...]:
        raise NotImplementedError

    @abstractmethod
    def upsert_profile(self, ticker: str, profile: EtfProfile) -> None:
        raise NotImplementedError
