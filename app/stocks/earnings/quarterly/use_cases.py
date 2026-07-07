"""Application use cases for the quarterly-earnings slice.

Two actions, both pure orchestration over the ports so they run offline in tests against
hand-written fakes and know nothing of yfinance, HTTP, or SQLAlchemy:

- ``GetQuarterlyEarnings`` — the read path. Normalizes the symbol and returns the
  timeline through the ``QuarterlyEarningsProvider`` (wired in production as the DB cache
  over yfinance, so the read hits Yahoo only on a miss).
- ``SyncQuarterlyEarnings`` — the out-of-band refresh. Walks the already-stored rows
  stalest-first and renews them from the live provider, so users see current quarters
  without a request ever waiting on a vendor round-trip. Invoked by the cron endpoint.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.stocks.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.stocks.earnings.quarterly.ports import QuarterlyEarningsProvider
from app.stocks.earnings.quarterly.repository import QuarterlyEarningsRepository
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.progress import iter_with_progress

logger = logging.getLogger(__name__)

# How many symbols the sweep fetches from Yahoo at once. Each symbol is a handful of blocking
# HTTP round-trips, so a small thread pool overlaps the waiting and cuts a full sweep from tens
# of minutes to a few — while the DB writes stay serial on the one session. Kept modest to stay
# gentle on Yahoo's per-IP tolerance; yfinance_session's pacing knob (YF_MIN_REQUEST_INTERVAL_MS)
# caps the aggregate request rate if it ever needs dialling back.
_DEFAULT_SYNC_WORKERS = 8


def _normalize_symbol(symbol: str) -> str:
    """Trim/upper-case the ticker and reject obvious junk, once, at the edge of the use
    case — so every layer below sees a clean symbol. Mirrors the stocks slice's guard."""
    normalized = (symbol or "").strip().upper()
    if not normalized:
        raise ValueError("A stock symbol is required.")
    if not normalized.isalpha() or len(normalized) > 5:
        # Simple guard; real tickers are 1-5 letters (ignoring class suffixes).
        raise ValueError(f"'{symbol}' is not a valid stock symbol.")
    return normalized


class GetQuarterlyEarnings:
    """Use case: retrieve a stock's per-quarter earnings timeline by its symbol.

    Best-effort: an uncovered symbol yields an empty timeline rather than an error, so
    the endpoint can present an empty result instead of a 404.
    """

    def __init__(self, provider: QuarterlyEarningsProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> QuarterlyEarningsTimeline:
        return self._provider.get_quarterly_earnings(_normalize_symbol(symbol))


@dataclass(frozen=True)
class QuarterlyEarningsSyncReport:
    """The outcome of one refresh run: how many stocks were renewed, how many the
    provider couldn't serve this run (or returned empty for), and the per-run cap
    (``None`` when the run was uncapped)."""

    refreshed: int
    failed: int
    limit: int | None


class SyncQuarterlyEarnings:
    """Renew stored quarterly earnings from the live source, most-stale stocks first — and
    **seed** stocks not yet cached (never-fetched anchor stocks come first)."""

    def __init__(
        self,
        provider: QuarterlyEarningsProvider,
        repository: QuarterlyEarningsRepository,
        *,
        max_workers: int = _DEFAULT_SYNC_WORKERS,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._max_workers = max(1, max_workers)

    def execute(self, *, limit: int | None = None) -> QuarterlyEarningsSyncReport:
        """Refresh up to ``limit`` stocks most in need of it (un-cached first, then stalest);
        ``limit=None`` (the default) processes every stock in the anchor. Returns a summary.
        Never raises for a single symbol's failure — the run continues and the failure is
        counted, so one bad symbol doesn't abort the whole sweep."""
        effective = None if limit is None else max(1, limit)
        refreshed = 0
        failed = 0
        targets = self._repository.refresh_targets(effective)
        # Each symbol's fetch is several blocking Yahoo round-trips, so fan the fetches out
        # across a small thread pool — but keep every DB write serial on the one (thread-unsafe)
        # session by consuming the futures here on the main thread. Consuming them in submission
        # order keeps the write/commit order stalest-first (so an interrupted run resumes
        # cleanly) and lets iter_with_progress log the same "<label>: P% (i/N)" lines as before.
        pool = ThreadPoolExecutor(max_workers=self._max_workers)
        try:
            futures = [
                pool.submit(self._provider.get_quarterly_earnings, target.symbol)
                for target in targets
            ]
            for target, future in iter_with_progress(
                list(zip(targets, futures)),
                logger=logger,
                label="quarterly-earnings sync",
            ):
                try:
                    timeline = future.result()
                except (StockNotFound, StockDataUnavailable):
                    # A symbol the vendor can't serve this run (outage, block, or dropped
                    # coverage) is left as-is and counted; the next run retries it.
                    failed += 1
                    continue
                # An empty live result must not wipe the stored window — the upsert rewrites
                # a stock's rows wholesale (delete-then-insert), so an empty write would
                # delete every quarter. Skip it and count a failure so the next run retries;
                # the stored rows keep serving in the meantime.
                if timeline.is_empty:
                    failed += 1
                    continue
                # A *degraded* fetch must not wipe stored figures either: the upsert rewrites the
                # whole window, so fill the fresh timeline's holes from the stored rows (missing
                # revenue actuals, quarters Yahoo dropped this run) before persisting. A newly-
                # seeded stock has nothing stored, so there's nothing to fill from. Reported
                # figures never change, so the stored values stay true.
                stored = self._repository.get(target.symbol)
                if stored is not None:
                    timeline = timeline.filled_from(stored)
                # Carry the stored name so a nameless refresh doesn't drop a known one.
                self._repository.upsert(target.symbol, target.name, timeline)
                refreshed += 1
        finally:
            # Happy path: every future is already done, so this is a no-op. On an unexpected
            # abort (e.g. a DB error mid-loop) cancel the not-yet-started fetches instead of
            # blocking the propagating exception behind the whole remaining sweep.
            pool.shutdown(cancel_futures=True)
        return QuarterlyEarningsSyncReport(
            refreshed=refreshed, failed=failed, limit=effective
        )
