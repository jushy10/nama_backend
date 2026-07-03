"""Database model + queries for the recommendations cache.

The persistence primitives for the slice: the SQLAlchemy model for the
``stock_recommendation_trends`` table this feature owns, plus simple, entity-free query
functions over it. The shared ``stocks`` anchor these rows hang off of lives in its own
slice, ``app/stocks/stocks/models.py`` (owned by no single feature), and is imported
here. The concrete repository (``db_repository.py``) is the only caller; it maps these
rows to and from the ``RecommendationTrend`` entity. Nothing here knows the domain entity
— this layer deals only in rows and columns, so it stays a thin data-access layer.

A time series: many rows per stock, one per monthly snapshot, keyed unique on
``(stock_id, period)``. Unlike the earnings tables, a refresh *merges* — it replaces the
months the source served and keeps earlier ones — so rows for one stock can carry
different ``fetched_at`` stamps; a stock's last refresh is the *max* stamp over its rows.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Sequence

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    UniqueConstraint,
    Uuid,
    delete,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base

# The shared ``stocks`` anchor + its get-or-create helper, re-exported so the repository
# reaches them as ``models.StockRecord`` / ``models.get_or_create_stock``.
from app.stocks.stocks.models import StockRecord, get_or_create_stock  # noqa: F401


class StockRecommendationTrendRecord(Base):
    """One monthly snapshot of one stock's analyst buy/hold/sell split.

    The five counts are how many sell-side analysts held each stance that month; all are
    non-null (an uncovered bucket is 0, and a symbol with no coverage at all simply has
    no row). ``period`` is the first day of the month the snapshot covers.
    """

    __tablename__ = "stock_recommendation_trends"
    __table_args__ = (
        UniqueConstraint(
            "stock_id",
            "period",
            name="uq_recommendation_trends_stock_period",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    period: Mapped[date] = mapped_column(Date, nullable=False)
    strong_buy: Mapped[int] = mapped_column(Integer, nullable=False)
    buy: Mapped[int] = mapped_column(Integer, nullable=False)
    hold: Mapped[int] = mapped_column(Integer, nullable=False)
    sell: Mapped[int] = mapped_column(Integer, nullable=False)
    strong_sell: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def trends_by_symbol(
    session: Session, symbol: str
) -> list[StockRecommendationTrendRecord]:
    """All stored trend rows for ``symbol`` (joined through the ``stocks`` anchor),
    ordered newest→oldest by period. Empty when nothing is stored for it yet."""
    return list(
        session.execute(
            select(StockRecommendationTrendRecord)
            .join(StockRecord, StockRecommendationTrendRecord.stock_id == StockRecord.id)
            .where(StockRecord.ticker == symbol)
            .order_by(StockRecommendationTrendRecord.period.desc())
        ).scalars()
    )


def delete_trends_for_periods(
    session: Session, stock_id: uuid.UUID, periods: Sequence[date]
) -> None:
    """Remove a stock's rows for exactly ``periods``, so a refresh can re-insert the
    months the source served while leaving earlier stored months intact (the merge)."""
    if not periods:
        return
    session.execute(
        delete(StockRecommendationTrendRecord).where(
            StockRecommendationTrendRecord.stock_id == stock_id,
            StockRecommendationTrendRecord.period.in_(periods),
        )
    )


def stalest_symbols(session: Session, limit: int) -> list[tuple[str, str | None]]:
    """Up to ``limit`` stored ``(symbol, name)`` pairs, least-recently-refreshed first.

    One entry per stock, ordered by the *newest* fetch stamp among its rows — the merge
    keeps old stamps on old months forever, so the min would pin a long-cached stock
    permanently stale; the max is when the stock was last actually refreshed. Only
    symbols that already have trend rows are returned, so a refresh walks what's
    actually cached; never-viewed symbols are filled lazily on first access instead.
    """
    rows = session.execute(
        select(StockRecord.ticker, StockRecord.name)
        .join(
            StockRecommendationTrendRecord,
            StockRecommendationTrendRecord.stock_id == StockRecord.id,
        )
        .group_by(StockRecord.id, StockRecord.ticker, StockRecord.name)
        .order_by(func.max(StockRecommendationTrendRecord.fetched_at).asc())
        .limit(limit)
    ).all()
    return [(row.ticker, row.name) for row in rows]
