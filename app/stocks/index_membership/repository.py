"""Abstract persistence port for the index-membership slice.

Dependency Inversion for storage: the sync use case is handed an ``IndexMembershipRepository``
and never knows whether it's backed by SQLAlchemy or an in-memory fake (tests) — it just calls
``reconcile``. The concrete SQLAlchemy implementation lives in ``db_repository.py``.

A *Repository*, not a *Provider*: membership is slow-moving reference data refreshed out of band
(the cron endpoint), not a live feed. It writes the flags straight onto the ``stocks`` anchor
(the ``in_sp500`` / ``in_nasdaq100`` columns) — there is no separate membership table.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.stocks.index_membership.entities import IndexMembershipSnapshot


@dataclass(frozen=True)
class IndexMembershipSyncCounts:
    """The row-level outcome of one reconcile, per index: how many anchors were newly *marked*
    as members (a flag flipped ``False``→``True``, including rows created for a member not yet
    in ``stocks``) and how many were *cleared* (a former member that dropped out of the index,
    flag flipped ``True``→``False``).

    An index the use case judged degraded (skipped) contributes zero to both — its flags are
    left exactly as they were.
    """

    sp500_marked: int
    sp500_cleared: int
    nasdaq100_marked: int
    nasdaq100_cleared: int


class IndexMembershipRepository(ABC):
    """A persistent store for index membership, reconciled by the sync — the shared ``stocks``
    anchor, in practice."""

    @abstractmethod
    def reconcile(
        self,
        snapshot: IndexMembershipSnapshot,
        *,
        sync_sp500: bool,
        sync_nasdaq100: bool,
    ) -> IndexMembershipSyncCounts:
        """Reconcile the ``stocks`` anchor to ``snapshot`` for each index flagged healthy.

        For an index whose ``sync_*`` is ``True``: mark every ticker in its snapshot set as a
        member (creating the anchor row if absent, never clobbering its other facts), and clear
        the flag on any stock currently marked that the snapshot no longer lists. For an index
        whose ``sync_*`` is ``False`` (the use case judged its fetch degraded), leave that
        index's flags untouched. Returns the per-index counts. Commits its own write.
        """
        raise NotImplementedError
