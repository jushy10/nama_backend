from abc import ABC, abstractmethod
from app.stocks.catalog.etfs.entities import EtfProfile


class EtfProfileAdapter(ABC):
    @abstractmethod
    def get_profile(self, symbol: str) -> EtfProfile:
        raise NotImplementedError
