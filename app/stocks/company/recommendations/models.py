from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Sequence

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
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
from app.stocks.catalog.anchor.models import StockRecord, get_or_create_stock  # noqa: F401


class StockRecommendationTrendRecord(Base):
    __tablename__ = "stock_analyst_trends"
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
    # Consensus 12-month price target — set on the latest row only, nullable elsewhere.
    target_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StockAnalystRatingChangeRecord(Base):
    __tablename__ = "stock_analyst_rating_changes"
    __table_args__ = (
        UniqueConstraint(
            "stock_id",
            "firm",
            "published_at",
            name="uq_analyst_rating_changes_stock_firm_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    firm: Mapped[str] = mapped_column(String(length=128), nullable=False)
    published_at: Mapped[date] = mapped_column(Date, nullable=False)
    action: Mapped[str | None] = mapped_column(String(length=16), nullable=True)
    from_grade: Mapped[str | None] = mapped_column(String(length=64), nullable=True)
    to_grade: Mapped[str | None] = mapped_column(String(length=64), nullable=True)
    target_current: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_prior: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def trends_by_symbol(
    session: Session, symbol: str
) -> list[StockRecommendationTrendRecord]:
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
    if not periods:
        return
    session.execute(
        delete(StockRecommendationTrendRecord).where(
            StockRecommendationTrendRecord.stock_id == stock_id,
            StockRecommendationTrendRecord.period.in_(periods),
        )
    )


def rating_changes_by_symbol(
    session: Session, symbol: str
) -> list[StockAnalystRatingChangeRecord]:
    return list(
        session.execute(
            select(StockAnalystRatingChangeRecord)
            .join(
                StockRecord,
                StockAnalystRatingChangeRecord.stock_id == StockRecord.id,
            )
            .where(StockRecord.ticker == symbol)
            .order_by(StockAnalystRatingChangeRecord.published_at.desc())
        ).scalars()
    )


def stalest_symbols(
    session: Session, limit: int | None = None
) -> list[tuple[str, str | None]]:
    max_fetched = func.max(StockRecommendationTrendRecord.fetched_at)
    stmt = (
        select(StockRecord.ticker, StockRecord.name)
        .outerjoin(
            StockRecommendationTrendRecord,
            StockRecommendationTrendRecord.stock_id == StockRecord.id,
        )
        .group_by(StockRecord.id, StockRecord.ticker, StockRecord.name)
        # un-cached (NULL stamp) first, then least-recently-refreshed — portable NULLs-first.
        .order_by(max_fetched.is_(None).desc(), max_fetched.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(row.ticker, row.name) for row in session.execute(stmt).all()]
