from __future__ import annotations

import logging
from dataclasses import dataclass

from app.stocks.entities import normalize_symbol
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.news.entities import StockNews
from app.stocks.news.ports import NewsProvider
from app.stocks.news.repository import NewsRepository
from app.stocks.progress import iter_with_progress

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: str) -> str:
    return normalize_symbol(symbol)


class GetStockNews:
    def __init__(self, provider: NewsProvider) -> None:
        self._provider = provider

    def execute(self, symbol: str) -> StockNews:
        return self._provider.get_news(_normalize_symbol(symbol))


@dataclass(frozen=True)
class NewsSyncReport:
    refreshed: int
    failed: int
    limit: int | None


class SyncStockNews:
    def __init__(self, provider: NewsProvider, repository: NewsRepository) -> None:
        self._provider = provider
        self._repository = repository

    def execute(self, *, limit: int | None = None) -> NewsSyncReport:
        effective = None if limit is None else max(1, limit)
        refreshed = 0
        failed = 0
        targets = self._repository.refresh_targets(effective)
        for target in iter_with_progress(targets, logger=logger, label="news sync"):
            try:
                news = self._provider.get_news(target.symbol)
            except (StockNotFound, StockDataUnavailable):
                # A symbol the vendor can't serve this run (outage, block, or dropped
                # coverage) is left as-is and counted; the next run retries it.
                failed += 1
                continue
            # An empty live result has nothing to merge (the upsert would write no rows,
            # so the stock's refresh stamp would never advance and it would jam the front
            # of the stale queue). Skip it and count a failure so the next run retries;
            # the stored articles keep serving in the meantime.
            if news.is_empty:
                failed += 1
                continue
            # Carry the stored name so a nameless refresh doesn't drop a known one.
            self._repository.upsert(target.symbol, target.name, news)
            refreshed += 1
        return NewsSyncReport(refreshed=refreshed, failed=failed, limit=effective)
