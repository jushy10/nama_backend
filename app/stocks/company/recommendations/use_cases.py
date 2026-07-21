from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.stocks.entities import normalize_symbol
from app.stocks.exceptions import StockDataUnavailable, StockNotFound
from app.stocks.progress import iter_with_progress
from app.stocks.company.recommendations.entities import (
    AnalystRatingChanges,
    AnalystRecommendations,
    FirmRating,
)
from app.stocks.company.recommendations.interfaces import (
    RatingChangeAdapter,
    RecommendationAdapter,
)
from app.stocks.company.recommendations.interfaces import (
    RatingChangesRepositoryAdapter,
    RecommendationsRepositoryAdapter,
    RefreshTarget,
)

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: str) -> str:
    return normalize_symbol(symbol)


@dataclass(frozen=True)
class AnalystInfo:
    symbol: str
    recommendations: AnalystRecommendations
    rating_changes: AnalystRatingChanges
    top_firms: tuple[FirmRating, ...] = ()


class GetStockAnalystInfo:
    def __init__(
        self,
        recommendations: RecommendationAdapter,
        rating_changes: RatingChangeAdapter,
        *,
        now: datetime | None = None,
    ) -> None:
        self._recommendations = recommendations
        self._rating_changes = rating_changes
        self._now = now  # injectable clock for tests; None → real now per call

    def execute(self, symbol: str) -> AnalystInfo:
        symbol = _normalize_symbol(symbol)
        # Trends are primary: their exceptions propagate to the endpoint's error mapping.
        recommendations = self._recommendations.get_recommendations(symbol)
        rating_changes = self._read_rating_changes(symbol)
        today = (self._now or datetime.now(timezone.utc)).date()
        return AnalystInfo(
            symbol=symbol,
            recommendations=recommendations,
            rating_changes=rating_changes,
            # The most credible covering firms, derived from the (best-effort) events — an
            # empty tuple when none is ranked. Only firms whose latest target is within the
            # last year count, so stale coverage doesn't linger on the card.
            top_firms=rating_changes.top_credible_firms(as_of=today),
        )

    def _read_rating_changes(self, symbol: str) -> AnalystRatingChanges:
        try:
            return self._rating_changes.get_rating_changes(symbol)
        except (StockNotFound, StockDataUnavailable):
            return AnalystRatingChanges(symbol)


@dataclass(frozen=True)
class RecommendationsSyncReport:
    refreshed: int
    failed: int
    limit: int | None
    rating_changes_refreshed: int = 0


class SyncRecommendations:
    def __init__(
        self,
        provider: RecommendationAdapter,
        repository: RecommendationsRepositoryAdapter,
        *,
        rating_change_provider: RatingChangeAdapter | None = None,
        rating_change_repository: RatingChangesRepositoryAdapter | None = None,
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._rating_change_provider = rating_change_provider
        self._rating_change_repository = rating_change_repository

    def execute(self, *, limit: int | None = None) -> RecommendationsSyncReport:
        effective = None if limit is None else max(1, limit)
        refreshed = 0
        failed = 0
        rating_changes_refreshed = 0
        targets = self._repository.refresh_targets(effective)
        for target in iter_with_progress(
            targets, logger=logger, label="recommendations sync"
        ):
            try:
                recommendations = self._provider.get_recommendations(target.symbol)
            except (StockNotFound, StockDataUnavailable):
                # A symbol the vendor can't serve this run (outage, block, or dropped
                # coverage) is left as-is and counted; the next run retries it.
                failed += 1
                continue
            # An empty live result has nothing to merge (the upsert would write no rows,
            # so the stock's refresh stamp would never advance and it would jam the front
            # of the stale queue). Skip it and count a failure so the next run retries;
            # the stored months keep serving in the meantime.
            if recommendations.is_empty:
                failed += 1
                continue
            # Carry the stored name so a nameless refresh doesn't drop a known one.
            self._repository.upsert(target.symbol, target.name, recommendations)
            refreshed += 1
            rating_changes_refreshed += self._sync_rating_changes(target)
        return RecommendationsSyncReport(
            refreshed=refreshed,
            failed=failed,
            limit=effective,
            rating_changes_refreshed=rating_changes_refreshed,
        )

    def _sync_rating_changes(self, target: RefreshTarget) -> int:
        provider = self._rating_change_provider
        repository = self._rating_change_repository
        if provider is None or repository is None:
            return 0
        try:
            changes = provider.get_rating_changes(target.symbol)
        except (StockNotFound, StockDataUnavailable):
            return 0
        if changes.is_empty:
            return 0
        try:
            repository.upsert(target.symbol, target.name, changes)
        except Exception:  # noqa: BLE001 — best-effort enrichment, never sink the sweep
            logger.warning(
                "rating-changes upsert failed for %s", target.symbol, exc_info=True
            )
            return 0
        return 1
