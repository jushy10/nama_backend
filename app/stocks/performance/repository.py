"""Abstract persistence port for the stock-performance slice.

The interface the ``SyncStockPerformance`` use case depends on — Dependency Inversion for
storage. The use case is handed a ``PerformanceRepository`` and never knows whether it's
backed by SQLAlchemy (the anchor columns) or an in-memory fake (tests); it just calls these
two methods. The concrete SQLAlchemy implementation lives in ``db_repository.py``.

A *Repository*, not a *Provider*: the trailing windows are slow-moving figures refreshed out
of band (the ``sync-stock-performance`` cron), not a live feed. Materializing them this way
keeps the heat map's read path off a year-of-daily-bars computation per index.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping

from app.stocks.entities import StockPerformance


class PerformanceRepository(ABC):
    """A persistent store for a stock's trailing performance, on the shared ``stocks`` anchor.

    Table-less: the six window figures are denormalized columns on ``stocks`` (like the
    fundamentals figures and the universe screen facts), so this port writes them straight onto
    the anchor row.
    """

    @abstractmethod
    def refresh_targets(self, limit: int | None) -> tuple[str, ...]:
        """Return the **screened** anchor tickers most in need of a performance refresh —
        **un-synced first** (a ``NULL`` ``performance_synced_at`` sorts ahead of every synced
        row), then the stalest-synced.

        Scoped to screened rows (``market_cap IS NOT NULL``): those are the universe the heat
        map colours and the search ranks, so there's no point fetching bars for an
        incidentally-known ticker. Includes rows never synced, so one sweep both *seeds* new
        coverage and renews stale rows. ``limit`` caps the batch; ``None`` returns every
        screened ticker (one sweep covers them all — the batched feed makes that cheap).
        """
        raise NotImplementedError

    @abstractmethod
    def set_performance(
        self, performance_by_ticker: Mapping[str, StockPerformance]
    ) -> int:
        """Overwrite each ticker's trailing windows on the anchor and stamp
        ``performance_synced_at``, in one commit; return how many rows were written.

        Writes only the tickers the batched feed returned — a target the feed had no history
        for is simply absent from the map and left untouched (and un-stamped), so it stays at
        the front of the stale-first queue and is retried next run rather than having a prior
        figure cleared. Overwrites every window column (including to ``None``) for a written
        ticker — a moving snapshot, not fill-once, so a window that no longer has enough
        history is cleared. A ticker with no anchor row is skipped. Commits once, so the sweep
        is durable independent of the request.

        Only two methods (no per-symbol read): the live source is a *batched* feed handed the
        whole work-list at once, unlike the earnings/fundamentals slices' one-call-per-stock
        sources — so the sweep is a single fetch, not a per-target loop.
        """
        raise NotImplementedError
