from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.domains.financials.earnings.quarterly.entities import QuarterlyEarningsTimeline
from app.domains.financials.earnings.quarterly.interfaces import QuarterlyEarningsAdapter
from app.domains.financials.earnings.quarterly.interfaces import (
    QuarterlyEarningsRepositoryAdapter,
    RefreshTarget,
)
from app.domains.shared.entities import normalize_symbol
from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.shared.progress import iter_with_progress

logger = logging.getLogger(__name__)

# How many symbols the sweep fetches from Yahoo at once. Each symbol is a handful of blocking
# HTTP round-trips, so a small thread pool overlaps the waiting and cuts a full sweep from tens
# of minutes to a few — while the DB writes stay serial on the one session. Kept modest to stay
# gentle on Yahoo's per-IP tolerance; yfinance_session's pacing knob (YF_MIN_REQUEST_INTERVAL_MS)
# caps the aggregate request rate if it ever needs dialling back.
_DEFAULT_SYNC_WORKERS = 8

# How many times a symbol blocked by a *transient* failure (``StockDataUnavailable`` — a Yahoo
# outage, or the intermittent data-centre-IP gate a fresh crumb can't clear) is attempted within
# one run before it's left for the next scheduled sync: the first pass plus up to two retries.
# The gate the retry targets lifts on a seconds-to-minutes timescale, so a symbol that failed
# early on a sweep often succeeds a pass later — recovering it here beats waiting a *week* for
# the next quarterly run. Genuine no-coverage (an empty timeline / ``StockNotFound``) is never
# retried, and a whole pass that recovers nothing stops the loop (see ``execute``), so a
# *persistent* block terminates after one wasted pass rather than hammering a blocked IP.
_DEFAULT_MAX_ATTEMPTS = 3


def _normalize_symbol(symbol: str) -> str:
    return normalize_symbol(symbol)


class GetQuarterlyEarnings:
    def __init__(self, provider: QuarterlyEarningsAdapter) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> QuarterlyEarningsTimeline:
        return self._provider.get_quarterly_earnings(_normalize_symbol(symbol))


@dataclass(frozen=True)
class QuarterlyEarningsSyncReport:
    refreshed: int
    failed: int
    limit: int | None


@dataclass(frozen=True)
class _PassOutcome:
    refreshed: int
    final_failed: int
    retryable: list[RefreshTarget]


class SyncQuarterlyEarnings:
    def __init__(
        self,
        provider: QuarterlyEarningsAdapter,
        repository: QuarterlyEarningsRepositoryAdapter,
        *,
        max_workers: int = _DEFAULT_SYNC_WORKERS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        retry_backoff_seconds: float = 0.0,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._max_workers = max(1, max_workers)
        # First attempt + retries; floored at 1 so a caller can disable retries with 1.
        self._max_attempts = max(1, max_attempts)
        # Pause between retry passes so an intermittent Yahoo block has time to lift. Defaults
        # to 0 (no sleep) so the offline tests don't wait; the production wiring dials it up.
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)

    def execute(self, *, limit: int | None = None) -> QuarterlyEarningsSyncReport:
        effective = None if limit is None else max(1, limit)
        # refresh_targets is read once, up front: the same stalest-first batch is retried, so
        # the retries can't spill past the per-run cap into fresh symbols.
        pending = self._repository.refresh_targets(effective)

        refreshed = 0
        final_failed = 0
        for attempt in range(self._max_attempts):
            label = (
                "quarterly-earnings sync"
                if attempt == 0
                else f"quarterly-earnings sync (retry {attempt})"
            )
            outcome = self._run_pass(pending, label=label)
            refreshed += outcome.refreshed
            final_failed += outcome.final_failed
            pending = outcome.retryable
            # Stop when nothing transient remains, on the final attempt, or when a whole pass
            # recovered *nothing* — a zero-progress pass means Yahoo is blocking persistently
            # this run, not intermittently, so more passes would only hammer a blocked IP (the
            # next scheduled sync retries the stragglers). This guard also means the retry logic
            # adds no extra load during a total block: the first pass proves the gate is
            # intermittent (some refreshed) before any retry runs.
            if not pending or outcome.refreshed == 0 or attempt == self._max_attempts - 1:
                break
            if self._retry_backoff_seconds > 0:
                time.sleep(self._retry_backoff_seconds)

        # Whatever still failed transiently after the last attempt joins the genuine
        # no-coverage failures in the run's failed tally.
        return QuarterlyEarningsSyncReport(
            refreshed=refreshed, failed=final_failed + len(pending), limit=effective
        )

    def _run_pass(self, targets: list[RefreshTarget], *, label: str) -> _PassOutcome:
        refreshed = 0
        final_failed = 0
        retryable: list[RefreshTarget] = []
        pool = ThreadPoolExecutor(max_workers=self._max_workers)
        try:
            futures = [
                pool.submit(self._provider.get_quarterly_earnings, target.symbol)
                for target in targets
            ]
            for target, future in iter_with_progress(
                list(zip(targets, futures)),
                logger=logger,
                label=label,
            ):
                try:
                    timeline = future.result()
                except StockDataUnavailable:
                    # A transient block (a Yahoo outage, or the intermittent data-centre-IP gate
                    # a fresh crumb can't clear) — hold it for another pass instead of counting
                    # it a failure now.
                    retryable.append(target)
                    continue
                except StockNotFound:
                    # Genuine no-coverage — a retry can't conjure data, so it's final.
                    final_failed += 1
                    continue
                # An empty live result must not wipe the stored window — the upsert rewrites
                # a stock's rows wholesale (delete-then-insert), so an empty write would
                # delete every quarter. Skip it and count a failure; it's coverage-shaped rather
                # than a raised block, so it's final here (the next scheduled run reseeds it),
                # and the stored rows keep serving in the meantime.
                if timeline.is_empty:
                    final_failed += 1
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
        return _PassOutcome(
            refreshed=refreshed, final_failed=final_failed, retryable=retryable
        )
