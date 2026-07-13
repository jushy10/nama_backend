"""Application use case for the stock-performance slice.

One action, pure orchestration over the ports so it runs offline in tests against hand-written
fakes and knows nothing of Alpaca, HTTP, or SQLAlchemy:

- ``SyncStockPerformance`` — the out-of-band populator. Reads the screened anchor stale-first,
  fetches every target's trailing windows from the batched live feed in **one** call, and lands
  them on the ``stocks`` anchor. Invoked by the (fire-and-forget) cron endpoint /
  ``python -m app.sync performance`` task.

Unlike the earnings/fundamentals sweeps (one source call per stock, so they loop and count
per-symbol), the live source here is a *batched* feed — one ``get_bulk_performance`` call for
the whole work-list (the adapter chunks internally). So this is a single fetch, not a loop: the
whole-batch outcome is what's best-effort (a total feed outage is swallowed to an empty write),
while a symbol the feed simply has no history for is absent from the result and left un-stamped
to retry next run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.performance.repository import PerformanceRepository
from app.stocks.ports import BulkPerformanceProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PerformanceSyncReport:
    """The outcome of one sync run. ``refreshed`` is how many stocks got their trailing windows
    written this run; ``skipped`` how many targets the feed returned no history for (or, when the
    whole batched feed was unavailable, every target) — those are left un-stamped so the next
    sweep retries them. ``limit`` echoes the cap the run was invoked with (``None`` = every
    screened stock)."""

    refreshed: int
    skipped: int
    limit: int | None


class SyncStockPerformance:
    """Refresh the ``stocks`` anchor's trailing performance windows from the batched live feed,
    stale-first."""

    def __init__(
        self,
        provider: BulkPerformanceProvider,
        repository: PerformanceRepository,
    ) -> None:
        self._provider = provider
        self._repository = repository

    def execute(self, *, limit: int | None = None) -> PerformanceSyncReport:
        """Fetch and store trailing windows for up to ``limit`` screened stocks (default: the
        whole screened universe), un-synced first then stalest.

        One batched feed call for the whole work-list (the adapter chunks it), so this is a
        single fetch rather than a per-stock loop. A total feed outage
        (``StockDataUnavailable`` — every chunk failed) is swallowed to an empty write and
        logged, so a transient Alpaca blip leaves the anchor untouched rather than clearing it;
        the trailing windows are best-effort colour on the heat map, never worth failing a
        sweep over. A symbol the feed returned no history for is absent from the result and
        left un-stamped, so it leads the stale queue next run.
        """
        targets = self._repository.refresh_targets(limit)
        if not targets:
            return PerformanceSyncReport(refreshed=0, skipped=0, limit=limit)
        try:
            performance_by_ticker = self._provider.get_bulk_performance(targets)
        except StockDataUnavailable:
            # The whole batched feed failed (every chunk). Write nothing — leave the anchor's
            # last-good windows in place — and count every target as skipped so it's retried.
            logger.warning(
                "stock performance sync: batched feed unavailable; leaving %d rows untouched",
                len(targets),
            )
            return PerformanceSyncReport(refreshed=0, skipped=len(targets), limit=limit)
        written = self._repository.set_performance(performance_by_ticker)
        return PerformanceSyncReport(
            refreshed=written, skipped=len(targets) - written, limit=limit
        )
