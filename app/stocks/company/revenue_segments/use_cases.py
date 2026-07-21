from __future__ import annotations

import logging
from dataclasses import dataclass

from app.stocks.entities import normalize_symbol
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.progress import iter_with_progress
from app.stocks.company.revenue_segments.entities import RevenueSegmentation
from app.stocks.company.revenue_segments.ports import RevenueSegmentsProvider
from app.stocks.company.revenue_segments.repository import RevenueSegmentsRepository

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: str) -> str:
    return normalize_symbol(symbol)


class GetRevenueSegments:
    def __init__(self, provider: RevenueSegmentsProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> RevenueSegmentation:
        return self._provider.get_revenue_segments(_normalize_symbol(symbol))


@dataclass(frozen=True)
class RevenueSegmentsSyncReport:
    refreshed: int
    failed: int
    limit: int | None


class SyncRevenueSegments:
    def __init__(
        self,
        provider: RevenueSegmentsProvider,
        repository: RevenueSegmentsRepository,
    ) -> None:
        self._provider = provider
        self._repository = repository

    def execute(self, *, limit: int | None = None) -> RevenueSegmentsSyncReport:
        effective = None if limit is None else max(1, limit)
        refreshed = 0
        failed = 0
        targets = self._repository.refresh_targets(effective)
        for target in iter_with_progress(
            targets, logger=logger, label="revenue-segments sync"
        ):
            try:
                segmentation = self._provider.get_revenue_segments(target.symbol)
            except (StockNotFound, StockDataUnavailable):
                # A symbol the source can't serve this run (a filer we can't map, or a
                # transport/bad-response failure) is left as-is and counted; the next run
                # retries it.
                failed += 1
                continue
            # An empty live result would merge no years (leaving the stored history untouched but
            # also never advancing the refresh stamp, jamming the front of the stale queue). Skip
            # it and count a failure so the next run retries; the stored rows keep serving.
            if segmentation.is_empty:
                failed += 1
                continue
            # Carry the stored name so a nameless refresh doesn't drop a known one.
            self._repository.upsert(target.symbol, target.name, segmentation)
            refreshed += 1
        return RevenueSegmentsSyncReport(
            refreshed=refreshed, failed=failed, limit=effective
        )
