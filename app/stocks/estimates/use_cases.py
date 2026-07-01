"""Application use case: refresh the stored forward analyst estimates.

The out-of-band half of the estimates cache. The stock endpoint fills a symbol's
estimates lazily the first time it's viewed (the DB-cache adapter); this walks the
already-stored rows stalest-first and renews them from the live provider, so users
see current consensus without a request ever waiting on a vendor round-trip.

FMP's free tier allows only ~250 calls/day — far fewer than the ~600 index
constituents — so a full-universe sweep isn't possible in one run. Instead each run
refreshes only rows already stored, oldest-fetched first, up to a cap. Combined with
lazy-fill, the symbols people actually look at stay current while staying under quota.

Pure orchestration over the ports — the live ``AnalystEstimatesProvider`` to fetch
and the ``AnalystEstimatesRepository`` to pick targets and store results — so it runs
offline in tests against hand-written fakes and knows nothing of FMP, HTTP, or
SQLAlchemy. This replaces the old ``scripts/sync_estimates.py``; the cron endpoint
(``cron_estimates_endpoints``) is what invokes it in production.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.stocks.estimates.estimates_ports import (
    AnalystEstimatesProvider,
    AnalystEstimatesRepository,
)
from app.stocks.exceptions import StockDataUnavailable, StockNotFound


@dataclass(frozen=True)
class EstimatesSyncReport:
    """The outcome of one refresh run: how many rows were renewed, how many the
    provider couldn't serve this run, and the per-run cap that was applied."""

    refreshed: int
    failed: int
    limit: int


class SyncAnalystEstimates:
    """Renew stored forward estimates from the live source, stalest rows first."""

    # Default rows per run. Held below FMP's ~250-calls/day free quota; the caller
    # (the cron endpoint) can override per invocation.
    DEFAULT_LIMIT = 200

    def __init__(
        self,
        provider: AnalystEstimatesProvider,
        repository: AnalystEstimatesRepository,
    ) -> None:
        self._provider = provider
        self._repository = repository

    def execute(self, *, limit: int | None = None) -> EstimatesSyncReport:
        """Refresh up to ``limit`` stalest rows (default ``DEFAULT_LIMIT``), returning
        a summary. Never raises for a single symbol's failure — the run continues and
        the failure is counted, so one bad symbol doesn't abort the whole sweep."""
        capped = self.DEFAULT_LIMIT if limit is None else max(1, limit)
        refreshed = 0
        failed = 0
        for target in self._repository.refresh_targets(capped):
            try:
                estimates = self._provider.get_estimates(target.symbol)
            except (StockNotFound, StockDataUnavailable):
                # A symbol the vendor can't serve this run (outage, blown quota, or
                # dropped coverage) is left as-is and counted; the next run retries it.
                failed += 1
                continue
            # Re-stamp even an empty result so an uncovered symbol isn't retried every
            # run; carry the stored name so a nameless refresh doesn't drop it.
            self._repository.upsert(target.symbol, target.name, estimates)
            refreshed += 1
        return EstimatesSyncReport(refreshed=refreshed, failed=failed, limit=capped)
