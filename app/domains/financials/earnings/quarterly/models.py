from __future__ import annotations

import uuid
from datetime import date, datetime

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
from app.domains.listings.anchor.models import StockRecord, get_or_create_stock  # noqa: F401


class StockQuarterlyEarningsRecord(Base):
    __tablename__ = "stock_quarterly_earnings"
    __table_args__ = (
        UniqueConstraint(
            "stock_id",
            "fiscal_year",
            "fiscal_quarter",
            name="uq_quarterly_earnings_stock_period",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    fiscal_quarter: Mapped[int] = mapped_column(Integer, nullable=False)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    report_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Market timing of the announcement (EarningsSession value: bmo/amc/during/unknown);
    # nullable for rows written before the column existed — the repo reads NULL as UNKNOWN.
    report_session: Mapped[str | None] = mapped_column(String(16), nullable=True)
    eps_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_surprise: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_surprise_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def quarters_by_symbol(
    session: Session, symbol: str
) -> list[StockQuarterlyEarningsRecord]:
    return list(
        session.execute(
            select(StockQuarterlyEarningsRecord)
            .join(StockRecord, StockQuarterlyEarningsRecord.stock_id == StockRecord.id)
            .where(StockRecord.ticker == symbol)
            .order_by(
                StockQuarterlyEarningsRecord.fiscal_year.asc(),
                StockQuarterlyEarningsRecord.fiscal_quarter.asc(),
            )
        ).scalars()
    )


def delete_quarters_for_stock(session: Session, stock_id: uuid.UUID) -> None:
    session.execute(
        delete(StockQuarterlyEarningsRecord).where(
            StockQuarterlyEarningsRecord.stock_id == stock_id
        )
    )


def stalest_symbols(
    session: Session, limit: int | None = None
) -> list[tuple[str, str | None]]:
    min_fetched = func.min(StockQuarterlyEarningsRecord.fetched_at)
    stmt = (
        select(StockRecord.ticker, StockRecord.name)
        .outerjoin(
            StockQuarterlyEarningsRecord,
            StockQuarterlyEarningsRecord.stock_id == StockRecord.id,
        )
        .group_by(StockRecord.id, StockRecord.ticker, StockRecord.name)
        # un-cached (NULL stamp) first, then stalest cached — portable NULLs-first ordering.
        .order_by(min_fetched.is_(None).desc(), min_fetched.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(row.ticker, row.name) for row in session.execute(stmt).all()]
