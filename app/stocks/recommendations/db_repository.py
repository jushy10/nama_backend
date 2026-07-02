"""Interface Adapter: the SQLAlchemy-backed RecommendationsRepository.

Implements the ``repository.py`` port against the database. Its job is the mapping the
use cases must not see: it converts the ``RecommendationTrend`` entities to and from the
ORM rows, and delegates every query to ``models.py``. Only this layer (and models) knows
the tables exist; the domain entities stay free of SQLAlchemy. ``upsert`` *merges* the
fetched months into the store (replace-matching-then-insert, keeping earlier months) and
commits its own write, so a successful cache fill is durable independent of the request.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.stocks.recommendations import models
from app.stocks.recommendations.entities import (
    AnalystRecommendations,
    RecommendationTrend,
)
from app.stocks.recommendations.models import StockRecommendationTrendRecord
from app.stocks.recommendations.repository import (
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


def _to_recommendations(
    symbol: str, rows: list[StockRecommendationTrendRecord]
) -> AnalystRecommendations:
    """Rebuild the run in its canonical order — newest snapshot first, the order the
    entity documents (``latest`` / ``direction`` read the front) — regardless of the row
    order the query returned."""
    trends = sorted(
        (_to_entity(row) for row in rows), key=lambda t: t.period, reverse=True
    )
    return AnalystRecommendations(symbol=symbol, trends=tuple(trends))


class SqlRecommendationsRepository(RecommendationsRepository):
    """Reads and writes the recommendations cache through a request-scoped session.

    Holds the session the endpoint injects via ``get_db``, maps rows to and from the
    ``RecommendationTrend`` entities, and delegates every query to ``models``. ``upsert``
    commits its own write so a successful cache fill is durable independent of the
    surrounding request.
    """

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
        for trend in recommendations.trends:
            self._session.add(
                StockRecommendationTrendRecord(
                    stock_id=stock.id,
                    period=trend.period,
                    strong_buy=trend.strong_buy,
                    buy=trend.buy,
                    hold=trend.hold,
                    sell=trend.sell,
                    strong_sell=trend.strong_sell,
                    fetched_at=now,
                )
            )
        self._session.commit()

    def refresh_targets(self, limit: int) -> list[RefreshTarget]:
        # Delegates the query to models (least-recently-refreshed first); this layer just
        # wraps each (symbol, name) pair in the domain-facing RefreshTarget.
        return [
            RefreshTarget(symbol, name)
            for symbol, name in models.stalest_symbols(self._session, limit)
        ]
