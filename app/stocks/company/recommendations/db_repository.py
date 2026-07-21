from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.stocks.company.recommendations import models
from app.stocks.company.recommendations.entities import (
    AnalystPriceTargets,
    AnalystRatingChanges,
    AnalystRecommendations,
    RatingChange,
    RecommendationTrend,
)
from app.stocks.company.recommendations.models import (
    StockAnalystRatingChangeRecord,
    StockRecommendationTrendRecord,
)
from app.stocks.company.recommendations.repository import (
    RatingChangesRepository,
    RecommendationsRepository,
    RefreshTarget,
)


def _to_entity(row: StockRecommendationTrendRecord) -> RecommendationTrend:
    return RecommendationTrend(
        period=row.period,
        strong_buy=row.strong_buy,
        buy=row.buy,
        hold=row.hold,
        sell=row.sell,
        strong_sell=row.strong_sell,
    )


def _to_price_targets(
    row: StockRecommendationTrendRecord,
) -> AnalystPriceTargets | None:
    targets = AnalystPriceTargets(
        mean=row.target_mean,
        high=row.target_high,
        low=row.target_low,
        median=row.target_median,
    )
    return None if targets.is_empty else targets


def _to_recommendations(
    symbol: str, rows: list[StockRecommendationTrendRecord]
) -> AnalystRecommendations:
    ordered = sorted(rows, key=lambda row: row.period, reverse=True)
    trends = tuple(_to_entity(row) for row in ordered)
    price_targets = _to_price_targets(ordered[0]) if ordered else None
    return AnalystRecommendations(
        symbol=symbol, trends=trends, price_targets=price_targets
    )


class SqlRecommendationsRepository(RecommendationsRepository):
    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the fetch stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get(self, symbol: str) -> AnalystRecommendations | None:
        rows = models.trends_by_symbol(self._session, symbol)
        if not rows:
            return None
        return _to_recommendations(symbol, rows)

    def upsert(
        self, symbol: str, name: str | None, recommendations: AnalystRecommendations
    ) -> None:
        stock = models.get_or_create_stock(self._session, symbol, name)

        # Merge, don't rewrite: clear only the months the source served this time, then
        # insert the fresh rows. A past month's split is a frozen fact and Yahoo serves
        # just the last few months, so earlier stored months stay — the table accumulates
        # a longer history than the source ever returns at once.
        periods = [trend.period for trend in recommendations.trends]
        models.delete_trends_for_periods(self._session, stock.id, periods)
        now = self._now()
        # The price target is a single current snapshot, so it's stamped onto the newest
        # month's row only; older rows keep null targets. (The newest month is always in the
        # served window, so it's rewritten with fresh targets each run.)
        newest_period = max(
            (trend.period for trend in recommendations.trends), default=None
        )
        targets = recommendations.price_targets
        for trend in recommendations.trends:
            on_newest = targets is not None and trend.period == newest_period
            self._session.add(
                StockRecommendationTrendRecord(
                    stock_id=stock.id,
                    period=trend.period,
                    strong_buy=trend.strong_buy,
                    buy=trend.buy,
                    hold=trend.hold,
                    sell=trend.sell,
                    strong_sell=trend.strong_sell,
                    target_mean=targets.mean if on_newest else None,
                    target_high=targets.high if on_newest else None,
                    target_low=targets.low if on_newest else None,
                    target_median=targets.median if on_newest else None,
                    fetched_at=now,
                )
            )
        self._session.commit()

    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        # Delegates the query to models (un-cached first, then least-recently-refreshed);
        # this layer just wraps each (symbol, name) pair in the domain-facing RefreshTarget.
        return [
            RefreshTarget(symbol, name)
            for symbol, name in models.stalest_symbols(self._session, limit)
        ]


def _to_rating_change(row: StockAnalystRatingChangeRecord) -> RatingChange:
    return RatingChange(
        firm=row.firm,
        published_at=row.published_at,
        action=row.action,
        from_grade=row.from_grade,
        to_grade=row.to_grade,
        target_current=row.target_current,
        target_prior=row.target_prior,
    )


def _to_rating_changes(
    symbol: str, rows: list[StockAnalystRatingChangeRecord]
) -> AnalystRatingChanges:
    changes = sorted(
        (_to_rating_change(row) for row in rows),
        key=lambda change: change.published_at,
        reverse=True,
    )
    return AnalystRatingChanges(symbol=symbol, changes=tuple(changes))


class SqlRatingChangesRepository(RatingChangesRepository):
    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the fetch stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get(self, symbol: str) -> AnalystRatingChanges | None:
        rows = models.rating_changes_by_symbol(self._session, symbol)
        if not rows:
            return None
        return _to_rating_changes(symbol, rows)

    def upsert(
        self, symbol: str, name: str | None, rating_changes: AnalystRatingChanges
    ) -> None:
        stock = models.get_or_create_stock(self._session, symbol, name)

        # Insert-only: an event is a frozen fact keyed on (firm, published_at), so add only
        # the ones not already stored and leave the rest untouched. The table thereby
        # accumulates a longer history than the source serves at once.
        existing = {
            (row.firm, row.published_at)
            for row in models.rating_changes_by_symbol(self._session, symbol)
        }
        now = self._now()
        for change in rating_changes.changes:
            key = (change.firm, change.published_at)
            if key in existing:
                continue
            existing.add(key)  # guard against duplicates within one fetch too
            self._session.add(
                StockAnalystRatingChangeRecord(
                    stock_id=stock.id,
                    firm=change.firm,
                    published_at=change.published_at,
                    action=change.action,
                    from_grade=change.from_grade,
                    to_grade=change.to_grade,
                    target_current=change.target_current,
                    target_prior=change.target_prior,
                    fetched_at=now,
                )
            )
        self._session.commit()
