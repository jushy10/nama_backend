from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.domains.shared.exceptions import StockDataUnavailable, StockNotFound
from app.domains.listings.fundamentals.interfaces import FundamentalsAdapter
from app.domains.listings.fundamentals.interfaces import FundamentalsRepositoryAdapter, RefreshTarget
from app.domains.shared.progress import iter_with_progress

logger = logging.getLogger(__name__)

# First attempt + retries. Yahoo's ``.info`` gate is intermittent per request, so a couple of
# re-passes over just the still-gated stocks (run after the whole sweep, so the gate has had
# the rest of the run to lift) recovers most of them. Floored at 1 by the constructor so a
# caller can disable retries.
_DEFAULT_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class FundamentalsSyncReport:
    refreshed: int
    failed: int
    limit: int | None


@dataclass(frozen=True)
class _PassOutcome:
    refreshed: int
    final_failed: int
    retryable: list[RefreshTarget]


class SyncFundamentals:
    def __init__(
        self,
        provider: FundamentalsAdapter,
        repository: FundamentalsRepositoryAdapter,
        *,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        retry_backoff_seconds: float = 0.0,
    ) -> None:
        self._provider = provider
        self._repository = repository
        # First attempt + retries; floored at 1 so a caller can disable retries with 1.
        self._max_attempts = max(1, max_attempts)
        # Pause between retry passes so an intermittent Yahoo block has time to lift. Defaults to
        # 0 (no sleep) so the offline tests don't wait; the production wiring dials it up.
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)

    def execute(self, *, limit: int | None = None) -> FundamentalsSyncReport:
        effective = None if limit is None else max(1, limit)
        # refresh_targets is read once, up front: the same stalest-first batch is retried, so the
        # retries can't spill past the per-run cap into fresh symbols.
        pending = self._repository.refresh_targets(effective)

        refreshed = 0
        final_failed = 0
        for attempt in range(self._max_attempts):
            label = (
                "fundamentals sync"
                if attempt == 0
                else f"fundamentals sync (retry {attempt})"
            )
            outcome = self._run_pass(pending, label=label)
            refreshed += outcome.refreshed
            final_failed += outcome.final_failed
            pending = outcome.retryable
            # Stop when nothing transient remains, on the final attempt, or when a whole pass
            # recovered *nothing* — a zero-progress pass means Yahoo is blocking persistently this
            # run, not intermittently, so more passes would only hammer a blocked IP (the next
            # scheduled sync retries the stragglers). This guard also means the retry logic adds
            # no extra load during a total block: the first pass proves the gate is intermittent
            # (some refreshed) before any retry runs.
            if not pending or outcome.refreshed == 0 or attempt == self._max_attempts - 1:
                break
            if self._retry_backoff_seconds > 0:
                time.sleep(self._retry_backoff_seconds)

        # Whatever still failed transiently after the last attempt joins the genuine no-coverage
        # failures in the run's failed tally.
        return FundamentalsSyncReport(
            refreshed=refreshed, failed=final_failed + len(pending), limit=effective
        )

    def _run_pass(self, targets: list[RefreshTarget], *, label: str) -> _PassOutcome:
        refreshed = 0
        final_failed = 0
        retryable: list[RefreshTarget] = []
        for target in iter_with_progress(targets, logger=logger, label=label):
            try:
                fundamentals = self._provider.get_fundamentals(target.symbol)
            except StockDataUnavailable:
                # A transient block (a Yahoo outage, or the intermittent data-centre-IP gate a
                # fresh crumb can't clear) — hold it for another pass instead of counting it a
                # failure now.
                retryable.append(target)
                continue
            except StockNotFound:
                # A genuinely unknown symbol — a retry can't conjure data, so it's final.
                final_failed += 1
                continue
            if fundamentals.is_empty:
                # A served ``.info`` that carried no figure. For a ≥$1B stock that's almost
                # always a swallowed gate the adapter's own crumb-retry couldn't clear (not
                # genuine no-coverage), so retry it like a raised block rather than freezing the
                # row as "fresh". The upsert is skipped either way, so nothing is overwritten.
                retryable.append(target)
                continue
            self._repository.upsert(target.symbol, target.name, fundamentals)
            refreshed += 1
        return _PassOutcome(
            refreshed=refreshed, final_failed=final_failed, retryable=retryable
        )
