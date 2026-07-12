"""Application use cases for the fundamentals slice.

One action, pure orchestration over the ports so it runs offline in tests against hand-written
fakes and knows nothing of Yahoo, HTTP, or SQLAlchemy:

- ``SyncFundamentals`` — the out-of-band populator. Walks the anchor stale-first (un-synced
  stocks first, then the oldest), fetches each stock's trailing fundamentals from the live
  source, and lands them on the ``stocks`` anchor. Invoked by the (fire-and-forget) cron
  endpoint / the ``python -m app.sync fundamentals`` task. Best-effort per stock: a single
  symbol the source can't serve is counted and skipped, never aborting the sweep, and left
  un-stamped so the next run retries it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.fundamentals.ports import FundamentalsProvider
from app.stocks.fundamentals.repository import FundamentalsRepository
from app.stocks.progress import iter_with_progress

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FundamentalsSyncReport:
    """The outcome of one sync run. ``refreshed`` is how many stocks got their fundamentals
    written this run; ``failed`` how many the source couldn't serve (an outage/block/uncovered
    symbol) — those are left un-stamped so the next sweep retries them. ``limit`` echoes the cap
    the run was invoked with (``None`` = the whole anchor)."""

    refreshed: int
    failed: int
    limit: int | None


class SyncFundamentals:
    """Refresh the ``stocks`` anchor's trailing fundamentals from the live source, stale-first."""

    def __init__(
        self,
        provider: FundamentalsProvider,
        repository: FundamentalsRepository,
    ) -> None:
        self._provider = provider
        self._repository = repository

    def execute(self, *, limit: int | None = None) -> FundamentalsSyncReport:
        """Fetch and store fundamentals for up to ``limit`` stocks (default: the whole anchor),
        un-synced first then stalest.

        Serial on purpose — one ``.info`` read per stock, paced by the task's
        ``YF_MIN_REQUEST_INTERVAL_MS`` so a burst doesn't trip Yahoo's IP gate mid-sweep. A
        single stock's failure (``StockNotFound`` / ``StockDataUnavailable``) is counted and the
        sweep continues; a served-but-hollow snapshot (``is_empty``) is skipped too (left
        un-stamped to retry). Only a real, non-empty snapshot is written and stamped.
        """
        refreshed = 0
        failed = 0
        targets = self._repository.refresh_targets(limit)
        for target in iter_with_progress(
            targets, logger=logger, label="fundamentals sync"
        ):
            try:
                fundamentals = self._provider.get_fundamentals(target.symbol)
            except (StockNotFound, StockDataUnavailable):
                # The source couldn't serve this symbol this run (outage/block/uncovered).
                # Leave the row untouched (un-stamped) and count it; the next run retries it.
                failed += 1
                continue
            if fundamentals.is_empty:
                # A served ``.info`` that carried no figure — nothing worth stamping. Leave it
                # un-synced so a later sweep tries again rather than freezing it as "fresh".
                continue
            self._repository.upsert(target.symbol, target.name, fundamentals)
            refreshed += 1
        return FundamentalsSyncReport(refreshed=refreshed, failed=failed, limit=limit)
