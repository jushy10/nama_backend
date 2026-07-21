from abc import ABC, abstractmethod

from app.stocks.insider_transactions.entities import InsiderActivity


class InsiderTransactionsProvider(ABC):
    @abstractmethod
    def get_insider_transactions(self, symbol: str) -> InsiderActivity:
        raise NotImplementedError
