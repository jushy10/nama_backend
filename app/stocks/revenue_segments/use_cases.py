"""Application use cases for the revenue-segments slice.

Two actions, both pure orchestration over the ports so they run offline in tests against
hand-written fakes and know nothing of SEC EDGAR, HTTP, or SQLAlchemy:

- ``GetRevenueSegments`` — the read path. Normalizes the symbol and returns the segmentation
  through the ``RevenueSegmentsProvider`` (wired in production as the DB cache over SEC, so the
  read hits EDGAR only on a miss).
- ``SyncRevenueSegments`` — the out-of-band refresh. Walks the already-stored rows
  least-recently-refreshed first (un-cached first, so it also *seeds* new coverage) and renews
  them from the live provider, so users see current segments without a request ever waiting on a
  multi-request filing walk. Invoked by the cron endpoint.

Serial by design (no thread pool, unlike the earnings syncs): each symbol is a few sequential
SEC round-trips and EDGAR asks automated clients to stay under 10 requests/second, so a serial
sweep is both simplest and naturally within that ceiling — the adapter's own request pacing is
the belt to this suspenders. Segment data is a frozen annual fact, so there are no in-run retry
passes either; a symbol the source can't serve this run is left for the next scheduled sweep.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.stocks.entities import normalize_symbol
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.progress import iter_with_progress
from app.stocks.revenue_segments.entities import RevenueSegmentation
from app.stocks.revenue_segments.ports import RevenueSegmentsProvider
from app.stocks.revenue_segments.repository import RevenueSegmentsRepository

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: str) -> str:
    """Trim/upper-case the ticker and reject obvious junk, once, at the edge of the use case —
    so every layer below sees a clean symbol. Mirrors the stocks slice's guard."""
    return normalize_symbol(symbol)


class GetRevenueSegments:
    """Use case: retrieve a company's revenue disaggregation by its symbol.

    Best-effort: a company that reports no disaggregation yields an empty segmentation rather
    than an error, so the endpoint can present an empty result instead of a 404.
    """

    def __init__(self, provider: RevenueSegmentsProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> RevenueSegmentation:
        return self._provider.get_revenue_segments(_normalize_symbol(symbol))


@dataclass(frozen=True)
class RevenueSegmentsSyncReport:
    """The outcome of one refresh run: how many stocks were renewed, how many the provider
    couldn't serve this run (or returned empty for), and the per-run cap (``None`` when the run
    was uncapped)."""

    refreshed: int
    failed: int
    limit: int | None


class SyncRevenueSegments:
    """Renew stored revenue segments from the live source, most-stale stocks first — and
    **seed** stocks not yet cached (never-fetched anchor stocks come first)."""

    def __init__(
        self,
        provider: RevenueSegmentsProvider,
        repository: RevenueSegmentsRepository,
    ) -> None:
        self._provider = provider
        self._repository = repository

    def execute(self, *, limit: int | None = None) -> RevenueSegmentsSyncReport:
        """Refresh up to ``limit`` stocks most in need of it (un-cached first, then stalest);
        ``limit=None`` (the default) processes every stock in the anchor. Returns a summary.
        Never raises for a single symbol's failure — the run continues and the failure is
        counted, so one bad symbol doesn't abort the whole sweep."""
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
