from abc import ABC, abstractmethod
from app.stocks.catalog.index_membership.entities import IndexMembershipSnapshot


class IndexMembershipAdapter(ABC):
    @abstractmethod
    def fetch(self) -> IndexMembershipSnapshot:
        raise NotImplementedError
