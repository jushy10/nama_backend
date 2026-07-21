from abc import ABC, abstractmethod

from app.stocks.index_membership.entities import IndexMembershipSnapshot


class IndexMembershipSource(ABC):
    @abstractmethod
    def fetch(self) -> IndexMembershipSnapshot:
        raise NotImplementedError
