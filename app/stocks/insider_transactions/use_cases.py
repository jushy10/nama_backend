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
    return normalize_symbol(symbol)


class GetInsiderTransactions:
    def __init__(self, provider: InsiderTransactionsProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> InsiderActivity:
        return self._provider.get_insider_transactions(_normalize_symbol(symbol))


@dataclass(frozen=True)
class InsiderTransactionsSyncReport:
    refreshed: int
    failed: int
    limit: int | None


class SyncInsiderTransactions:
    def __init__(
        self,
        provider: InsiderTransactionsProvider,
        repository: InsiderTransactionsRepository,
    ) -> None:
        self._provider = provider
        self._repository = repository

    def execute(self, *, limit: int | None = None) -> InsiderTransactionsSyncReport:
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
