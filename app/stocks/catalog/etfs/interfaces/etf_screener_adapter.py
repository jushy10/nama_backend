from abc import ABC, abstractmethod
from app.stocks.catalog.etfs.entities import ScreenedEtf


class EtfScreenerAdapter(ABC):
    @abstractmethod
    def screen(self, *, min_net_assets: float) -> tuple[ScreenedEtf, ...]:
        raise NotImplementedError
