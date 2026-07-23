from abc import ABC, abstractmethod
from datetime import date


class QuotaRepository(ABC):
    """Persistence for the per-client daily generation counter."""

    @abstractmethod
    def try_consume(self, pool: str, client_key: str, day: date, limit: int) -> bool:
        """Atomically spend one generation from the client's budget for `day`;
        False when the budget is already at `limit` (nothing is consumed)."""
