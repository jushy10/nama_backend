from abc import ABC, abstractmethod
from collections.abc import Sequence
from app.stocks.catalog.etfs.entities import EtfScreenIntent


class EtfScreenerQueryAdapter(ABC):
    @abstractmethod
    def translate(
        self,
        query: str,
        *,
        categories: Sequence[str],
    ) -> EtfScreenIntent:
        raise NotImplementedError
