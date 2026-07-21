from __future__ import annotations

import logging
from dataclasses import dataclass

from app.stocks.exceptions import StockDataUnavailable
from app.stocks.catalog.performance.repository import PerformanceRepository
from app.stocks.ports import BulkPerformanceProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PerformanceSyncReport:
    refreshed: int
    skipped: int
    limit: int | None


class SyncStockPerformance:
    def __init__(
        self,
        provider: BulkPerformanceProvider,
        repository: PerformanceRepository,
    ) -> None:
        self._provider = provider
        self._repository = repository

    def execute(self, *, limit: int | None = None) -> PerformanceSyncReport:
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
