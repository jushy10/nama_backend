from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.domains.listings.index_membership.entities import IndexMembershipSnapshot
from app.domains.listings.index_membership.interfaces import (
    IndexMembershipRepositoryAdapter,
    IndexMembershipSyncCounts,
)
from app.domains.listings.anchor.models import StockRecord, get_or_create_stock


class IndexMembershipRepositoryAdapterImpl(IndexMembershipRepositoryAdapter):
    def __init__(self, session: Session) -> None:
        self._session = session

    def reconcile(
        self,
        snapshot: IndexMembershipSnapshot,
        *,
        sync_sp500: bool,
        sync_nasdaq100: bool,
    ) -> IndexMembershipSyncCounts:
        sp500_marked = sp500_cleared = 0
        nasdaq100_marked = nasdaq100_cleared = 0
        if sync_sp500:
            sp500_marked, sp500_cleared = self._reconcile_index(
                StockRecord.in_sp500, snapshot.sp500
            )
        if sync_nasdaq100:
            nasdaq100_marked, nasdaq100_cleared = self._reconcile_index(
                StockRecord.in_nasdaq100, snapshot.nasdaq100
            )
        self._session.commit()
        return IndexMembershipSyncCounts(
            sp500_marked=sp500_marked,
            sp500_cleared=sp500_cleared,
            nasdaq100_marked=nasdaq100_marked,
            nasdaq100_cleared=nasdaq100_cleared,
        )

    def _reconcile_index(
        self, column: InstrumentedAttribute, members: frozenset[str]
    ) -> tuple[int, int]:
        # Clear ex-members: rows flagged True whose ticker isn't in the fresh set.
        cleared = self._session.execute(
            update(StockRecord)
            .where(column.is_(True), StockRecord.ticker.not_in(members))
            .values({column: False})
        ).rowcount
        # Mark current members: create the anchor if absent, and flip the flag only when it
        # isn't already set — so ``marked`` counts genuine transitions, not re-affirmations.
        marked = 0
        for ticker in members:
            anchor = get_or_create_stock(self._session, ticker, None)
            if not getattr(anchor, column.key):
                setattr(anchor, column.key, True)
                marked += 1
        return marked, cleared
