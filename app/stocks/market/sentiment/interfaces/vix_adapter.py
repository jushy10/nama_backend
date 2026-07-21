from abc import ABC, abstractmethod
from app.stocks.market.sentiment.entities import VixSnapshot


class VixAdapter(ABC):
    @abstractmethod
    def get_vix(self) -> VixSnapshot:
        raise NotImplementedError
