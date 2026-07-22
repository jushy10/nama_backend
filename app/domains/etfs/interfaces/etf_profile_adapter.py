from abc import ABC, abstractmethod
from app.domains.etfs.entities import EtfProfile


class EtfProfileAdapter(ABC):
    @abstractmethod
    def get_profile(self, symbol: str) -> EtfProfile:
        raise NotImplementedError
