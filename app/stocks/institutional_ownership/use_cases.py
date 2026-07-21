from __future__ import annotations

import logging
from dataclasses import dataclass

from app.stocks.entities import normalize_symbol
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.institutional_ownership.entities import InstitutionalOwnership
from app.stocks.institutional_ownership.ports import InstitutionalOwnershipProvider
from app.stocks.institutional_ownership.repository import (
    InstitutionalOwnershipRepository,
)
from app.stocks.progress import iter_with_progress

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: str) -> str:
    return normalize_symbol(symbol)


class GetInstitutionalOwnership:
    def __init__(self, provider: InstitutionalOwnershipProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> InstitutionalOwnership:
        return self._provider.get_institutional_ownership(_normalize_symbol(symbol))


@dataclass(frozen=True)
class InstitutionalOwnershipSyncReport:
    refreshed: int
    failed: int
    limit: int | None


class SyncInstitutionalOwnership:
    def __init__(
        self,
        provider: InstitutionalOwnershipProvider,
        repository: InstitutionalOwnershipRepository,
    ) -> None:
        self._provider = provider
        self._repository = repository

    def execute(self, *, limit: int | None = None) -> InstitutionalOwnershipSyncReport:
        effective = None if limit is None else max(1, limit)
        refreshed = 0
        failed = 0
        targets = self._repository.refresh_targets(effective)
        for target in iter_with_progress(
            targets, logger=logger, label="institutional-ownership sync"
        ):
            try:
                ownership = self._provider.get_institutional_ownership(target.symbol)
            except (StockNotFound, StockDataUnavailable):
                # A symbol the vendor can't serve this run (outage, block, or dropped coverage) is
                # left as-is and counted; the next run retries it.
                failed += 1
                continue
            # An empty live result has nothing to merge (the upsert would write no holder rows, so
            # the stock's refresh stamp would never advance and it would jam the front of the stale
            # queue). Skip it and count a failure so the next run retries; the stored rows keep
            # serving in the meantime.
            if ownership.is_empty:
                failed += 1
                continue
            # Carry the stored name so a nameless refresh doesn't drop a known one.
            self._repository.upsert(target.symbol, target.name, ownership)
            refreshed += 1
        return InstitutionalOwnershipSyncReport(
            refreshed=refreshed, failed=failed, limit=effective
        )
