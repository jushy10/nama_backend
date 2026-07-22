from abc import ABC, abstractmethod
from app.domains.profile.logo.entities import Logo


class LogoAdapter(ABC):
    @abstractmethod
    def get_logo(self, symbol: str) -> Logo:
        raise NotImplementedError
