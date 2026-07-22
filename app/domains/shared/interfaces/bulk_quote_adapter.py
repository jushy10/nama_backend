from abc import ABC, abstractmethod
from collections.abc import Sequence
from app.domains.shared.entities import Quote


class BulkQuoteAdapter(ABC):
    @abstractmethod
    def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        raise NotImplementedError
