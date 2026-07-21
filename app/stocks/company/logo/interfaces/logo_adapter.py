from abc import ABC, abstractmethod
from app.stocks.company.logo.entities import Logo


class LogoAdapter(ABC):
    @abstractmethod
    def get_logo(self, symbol: str) -> Logo:
        raise NotImplementedError
