from abc import ABC, abstractmethod
from app.domains.ownership.insider_transactions.entities import InsiderActivity


class InsiderTransactionsAdapter(ABC):
    @abstractmethod
    def get_insider_transactions(self, symbol: str) -> InsiderActivity:
        raise NotImplementedError
