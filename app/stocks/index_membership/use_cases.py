"""Application use cases for the index-membership slice.

One action, pure orchestration over the ports so it runs offline in tests against hand-written
fakes and knows nothing of Finnhub, HTTP, or SQLAlchemy:

- ``SyncIndexMembership`` — the out-of-band reconciler. Reads current index membership from the
  live source and reconciles it onto the ``stocks`` anchor (marking members, clearing
  drop-outs). Invoked by the cron endpoint. Guarded per index so a truncated/blocked fetch (an
  implausibly short list) is skipped rather than clearing every member.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.stocks.index_membership.ports import IndexMembershipSource
from app.stocks.index_membership.repository import IndexMembershipRepository


@dataclass(frozen=True)
class IndexMembershipSyncReport:
    """The outcome of one reconcile run. Per index: how many members the source returned, the
    anchors marked / cleared by the reconcile, and ``skipped`` — ``True`` when the fetched list
    was empty or implausibly short (a truncated or blocked fetch), so that index was left
    untouched (its marked/cleared are then both zero)."""

    sp500_members: int
    sp500_marked: int
    sp500_cleared: int
    sp500_skipped: bool
    nasdaq100_members: int
    nasdaq100_marked: int
    nasdaq100_cleared: int
    nasdaq100_skipped: bool


class SyncIndexMembership:
    """Reconcile the ``stocks`` anchor's index-membership flags from the live source."""

    # Below this many names a fetched index list is treated as truncated or blocked (real
    # counts are ~503 and ~101), so that index's reconcile is skipped — a bad scrape must not
    # clear a whole index. The source also raises when it has nothing at all (which propagates);
    # these guard a *degraded* per-index result.
    MIN_PLAUSIBLE_SP500 = 400
    MIN_PLAUSIBLE_NASDAQ100 = 90

    def __init__(
        self,
        source: IndexMembershipSource,
        repository: IndexMembershipRepository,
    ) -> None:
        self._source = source
        self._repository = repository

    def execute(self) -> IndexMembershipSyncReport:
        """Fetch current membership and reconcile each index that came back plausibly complete.

        A hard source failure (``StockDataUnavailable`` — nothing fetched at all) propagates to
        the caller. A *degraded* single index — fewer than its ``MIN_PLAUSIBLE_*`` names — is
        skipped, so a partial/blocked list isn't reconciled (which would clear real members).
        """
        snapshot = self._source.fetch()
        sync_sp500 = len(snapshot.sp500) >= self.MIN_PLAUSIBLE_SP500
        sync_nasdaq100 = len(snapshot.nasdaq100) >= self.MIN_PLAUSIBLE_NASDAQ100
        counts = self._repository.reconcile(
            snapshot, sync_sp500=sync_sp500, sync_nasdaq100=sync_nasdaq100
        )
        return IndexMembershipSyncReport(
            sp500_members=len(snapshot.sp500),
            sp500_marked=counts.sp500_marked,
            sp500_cleared=counts.sp500_cleared,
            sp500_skipped=not sync_sp500,
            nasdaq100_members=len(snapshot.nasdaq100),
            nasdaq100_marked=counts.nasdaq100_marked,
            nasdaq100_cleared=counts.nasdaq100_cleared,
            nasdaq100_skipped=not sync_nasdaq100,
        )
