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

# The shared ``stocks`` anchor + its get-or-create helper, re-exported so the repository reaches
# them as ``models.StockRecord`` / ``models.get_or_create_stock``.
from app.stocks.catalog.anchor.models import StockRecord, get_or_create_stock  # noqa: F401


class StockRevenueSegmentRecord(Base):
    __tablename__ = "stock_revenue_segments"
    __table_args__ = (
        UniqueConstraint(
            "stock_id",
            "fiscal_year",
            "axis",
            "member",
            name="uq_revenue_segments_stock_year_axis_member",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    axis: Mapped[str] = mapped_column(String(32), nullable=False)
    member: Mapped[str] = mapped_column(String(160), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def segments_by_symbol(
    session: Session, symbol: str
) -> list[StockRevenueSegmentRecord]:
    return list(
        session.execute(
            select(StockRevenueSegmentRecord)
            .join(StockRecord, StockRevenueSegmentRecord.stock_id == StockRecord.id)
            .where(StockRecord.ticker == symbol)
            .order_by(
                StockRevenueSegmentRecord.fiscal_year.desc(),
                StockRevenueSegmentRecord.axis,
                StockRevenueSegmentRecord.value.desc(),
            )
        ).scalars()
    )


def delete_years_for_stock(
    session: Session, stock_id: uuid.UUID, fiscal_years: Sequence[int]
) -> None:
    if not fiscal_years:
        return
    session.execute(
        delete(StockRevenueSegmentRecord).where(
            StockRevenueSegmentRecord.stock_id == stock_id,
            StockRevenueSegmentRecord.fiscal_year.in_(list(fiscal_years)),
        )
    )


def prune_to_newest_years(session: Session, stock_id: uuid.UUID, keep: int) -> None:
    years = list(
        session.execute(
            select(StockRevenueSegmentRecord.fiscal_year)
            .where(StockRevenueSegmentRecord.stock_id == stock_id)
            .distinct()
            .order_by(StockRevenueSegmentRecord.fiscal_year.desc())
        ).scalars()
    )
    surplus = years[keep:]
    if surplus:
        session.execute(
            delete(StockRevenueSegmentRecord).where(
                StockRevenueSegmentRecord.stock_id == stock_id,
                StockRevenueSegmentRecord.fiscal_year.in_(surplus),
            )
        )


def stalest_symbols(
    session: Session, limit: int | None = None
) -> list[tuple[str, str | None]]:
    max_fetched = func.max(StockRevenueSegmentRecord.fetched_at)
    stmt = (
        select(StockRecord.ticker, StockRecord.name)
        .outerjoin(
            StockRevenueSegmentRecord,
            StockRevenueSegmentRecord.stock_id == StockRecord.id,
        )
        .group_by(StockRecord.id, StockRecord.ticker, StockRecord.name)
        # un-cached (NULL stamp) first, then least-recently-refreshed — portable NULLs-first.
        .order_by(max_fetched.is_(None).desc(), max_fetched.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(row.ticker, row.name) for row in session.execute(stmt).all()]
