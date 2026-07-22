from abc import ABC, abstractmethod
from datetime import date
from app.domains.ownership.congress.entities import CongressActivity, CongressTrade
from app.domains.ownership.congress.interfaces.types import RefreshTarget


class CongressTradesRepositoryAdapter(ABC):
    @abstractmethod
    def get(self, symbol: str) -> CongressActivity | None:
        raise NotImplementedError

    @abstractmethod
    def upsert(self, symbol: str, name: str | None, activity: CongressActivity) -> None:
        raise NotImplementedError

    @abstractmethod
    def recent_market_activity(
        self, *, since: date | None, limit: int, offset: int
    ) -> tuple[list[CongressTrade], int]:
        raise NotImplementedError

    @abstractmethod
    def market_trades_in_window(self, *, since: date | None) -> list[CongressTrade]:
        raise NotImplementedError

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        raise NotImplementedError
