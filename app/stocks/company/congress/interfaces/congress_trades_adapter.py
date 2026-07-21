from abc import ABC, abstractmethod
from app.stocks.company.congress.entities import CongressTrade


class CongressTradesAdapter(ABC):
    @abstractmethod
    def fetch_recent_trades(self) -> tuple[CongressTrade, ...]:
        raise NotImplementedError
