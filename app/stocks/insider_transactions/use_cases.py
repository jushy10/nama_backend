"""Application use cases for the insider-transactions slice.

Two actions, both pure orchestration over the ports so they run offline in tests against
hand-written fakes and know nothing of SEC EDGAR, HTTP, or SQLAlchemy:

- ``GetInsiderTransactions`` — the read path. Normalizes the symbol and returns the activity
  through the ``InsiderTransactionsProvider`` (wired in production as the DB cache over SEC, so
  the read hits EDGAR only on a cold miss).
- ``SyncInsiderTransactions`` — the out-of-band refresh. Walks the anchor least-recently-refreshed
  first (un-cached first, so it also *seeds* new coverage) and renews each stock from the live
  provider, so users see current insider activity without a request ever waiting on a
  multi-request-per-symbol filing walk. Invoked by the weekly cron.

Serial by design (no thread pool, like the revenue-segments sync): each symbol is several
sequential SEC round-trips and EDGAR asks automated clients to stay under 10 requests/second, so
a serial sweep is both simplest and naturally within that ceiling — the adapter's own request
pacing is the belt to this suspenders. A filed transaction is a frozen fact, so there are no
in-run retry passes either; a symbol the source can't serve this run is left for the next
scheduled sweep.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.stocks.entities import normalize_symbol
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.insider_transactions.entities import InsiderActivity
from app.stocks.insider_transactions.ports import InsiderTransactionsProvider
from app.stocks.insider_transactions.repository import InsiderTransactionsRepository
from app.stocks.progress import iter_with_progress

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: str) -> str:
    """Trim/upper-case the ticker and reject obvious junk, once, at the edge of the use case —
    so every layer below sees a clean symbol. Mirrors the stocks slice's guard."""
    return normalize_symbol(symbol)


class GetInsiderTransactions:
    """Use case: retrieve a stock's recent insider (Form 4) transactions by its symbol.

    Best-effort: a stock with no recent insider activity yields an empty activity rather than an
    error, so the endpoint can present an empty result instead of a 404.
    """

    def __init__(self, provider: InsiderTransactionsProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> InsiderActivity:
        return self._provider.get_insider_transactions(_normalize_symbol(symbol))


@dataclass(frozen=True)
class InsiderTransactionsSyncReport:
    """The outcome of one refresh run: how many stocks were renewed, how many the provider
    couldn't serve this run (or returned empty for), and the per-run cap (``None`` when the run
    was uncapped)."""

    refreshed: int
    failed: int
    limit: int | None


class SyncInsiderTransactions:
    """Renew stored insider transactions from the live source, most-stale stocks first — and
    **seed** stocks not yet cached (never-fetched anchor stocks come first)."""

    def __init__(
        self,
        provider: InsiderTransactionsProvider,
        repository: InsiderTransactionsRepository,
    ) -> None:
        self._provider = provider
        self._repository = repository

    def execute(self, *, limit: int | None = None) -> InsiderTransactionsSyncReport:
        """Refresh up to ``limit`` stocks most in need of it (un-cached first, then stalest);
        ``limit=None`` (the default) processes every stock in the anchor. Returns a summary.
        Never raises for a single symbol's failure — the run continues and the failure is
        counted, so one bad symbol doesn't abort the whole sweep."""
        effective = None if limit is None else max(1, limit)
        refreshed = 0
        failed = 0
        targets = self._repository.refresh_targets(effective)
        for target in iter_with_progress(
            targets, logger=logger, label="insider-transactions sync"
        ):
            try:
                activity = self._provider.get_insider_transactions(target.symbol)
            except (StockNotFound, StockDataUnavailable):
                # A symbol the source can't serve this run (a filer we can't map, or a
                # transport/bad-response failure) is left as-is and counted; the next run
                # retries it.
                failed += 1
                continue
            # An empty live result would merge no transactions (leaving the stored feed untouched
            # but also never advancing the refresh stamp, jamming the front of the stale queue).
            # Skip it and count a failure so the next run retries; the stored rows keep serving.
            if activity.is_empty:
                failed += 1
                continue
            # Carry the stored name so a nameless refresh doesn't drop a known one.
            self._repository.upsert(target.symbol, target.name, activity)
            refreshed += 1
        return InsiderTransactionsSyncReport(
            refreshed=refreshed, failed=failed, limit=effective
        )
