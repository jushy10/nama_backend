from abc import ABC, abstractmethod
from collections.abc import Sequence
from app.domains.listings.universe.entities import ScreenIntent


class ScreenerQueryAdapter(ABC):
    @abstractmethod
    def translate(
        self,
        query: str,
        *,
        sectors: Sequence[str],
        industries: Sequence[str],
    ) -> ScreenIntent:
        raise NotImplementedError
