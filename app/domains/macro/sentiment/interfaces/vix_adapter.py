from abc import ABC, abstractmethod
from app.domains.macro.sentiment.entities import VixSnapshot


class VixAdapter(ABC):
    @abstractmethod
    def get_vix(self) -> VixSnapshot:
        raise NotImplementedError
