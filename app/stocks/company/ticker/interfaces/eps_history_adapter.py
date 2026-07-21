from abc import ABC, abstractmethod
from app.stocks.company.ticker.entities import ReportedEps


class EpsHistoryAdapter(ABC):
    @abstractmethod
    def get_eps_history(self, symbol: str) -> tuple[ReportedEps, ...]:
        raise NotImplementedError
