from abc import ABC, abstractmethod
from app.domains.ownership.congress.entities import CongressTrade


class CongressTradesAdapter(ABC):
    @abstractmethod
    def fetch_recent_trades(self) -> tuple[CongressTrade, ...]:
        raise NotImplementedError
