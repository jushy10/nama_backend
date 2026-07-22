from abc import ABC, abstractmethod
from app.domains.listings.index_membership.entities import IndexMembershipSnapshot
from app.domains.listings.index_membership.interfaces.types import IndexMembershipSyncCounts


class IndexMembershipRepositoryAdapter(ABC):
    @abstractmethod
    def reconcile(
        self,
        snapshot: IndexMembershipSnapshot,
        *,
        sync_sp500: bool,
        sync_nasdaq100: bool,
    ) -> IndexMembershipSyncCounts:
        raise NotImplementedError
