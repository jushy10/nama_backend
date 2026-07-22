from abc import ABC, abstractmethod
from app.domains.macro.sentiment.entities import FearGreedSnapshot


class FearGreedAdapter(ABC):
    @abstractmethod
    def get_fear_greed(self) -> FearGreedSnapshot:
        raise NotImplementedError
