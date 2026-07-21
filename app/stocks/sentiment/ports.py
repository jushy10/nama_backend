from abc import ABC, abstractmethod

from app.stocks.sentiment.entities import FearGreedSnapshot, VixSnapshot


class VixProvider(ABC):
    @abstractmethod
    def get_vix(self) -> VixSnapshot:
        raise NotImplementedError


class FearGreedProvider(ABC):
    @abstractmethod
    def get_fear_greed(self) -> FearGreedSnapshot:
        raise NotImplementedError
