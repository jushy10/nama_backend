from abc import ABC, abstractmethod
from collections.abc import Sequence
from app.stocks.entities import Quote


class BulkQuoteAdapter(ABC):
    @abstractmethod
    def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        raise NotImplementedError
