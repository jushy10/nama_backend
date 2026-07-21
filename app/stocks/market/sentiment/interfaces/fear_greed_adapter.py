from abc import ABC, abstractmethod
from app.stocks.market.sentiment.entities import FearGreedSnapshot


class FearGreedAdapter(ABC):
    @abstractmethod
    def get_fear_greed(self) -> FearGreedSnapshot:
        raise NotImplementedError
