from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.stocks.catalog.index_membership.entities import IndexMembershipSnapshot


@dataclass(frozen=True)
class IndexMembershipSyncCounts:
    sp500_marked: int
    sp500_cleared: int
    nasdaq100_marked: int
    nasdaq100_cleared: int


class IndexMembershipRepository(ABC):
    @abstractmethod
    def reconcile(
        self,
        snapshot: IndexMembershipSnapshot,
        *,
        sync_sp500: bool,
        sync_nasdaq100: bool,
    ) -> IndexMembershipSyncCounts:
        raise NotImplementedError
