"""Database model + queries for the quarterly-earnings cache.

The persistence primitives for the slice: the SQLAlchemy model for the
``stock_quarterly_earnings`` table this feature owns, plus simple, entity-free query
functions over it. The shared ``stocks`` anchor these rows hang off of lives in its own
slice, ``app/stocks/stocks/models.py`` (owned by no single feature), and is imported
here. The concrete repository (``db_repository.py``) is the only caller; it maps these
rows to and from the ``QuarterlyEarnings`` entity. Nothing here knows the domain entity
— this layer deals only in rows and columns, so it stays a thin data-access layer.

Unlike ``stock_analyst_estimates`` (one wide row per stock), this is a time series: many
rows per stock, one per fiscal quarter, keyed unique on ``(stock_id, fiscal_year,
fiscal_quarter)``. A refresh rewrites a stock's whole window at once (delete-then-insert),
so every row for a stock shares one ``fetched_at``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
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


class StockQuarterlyEarningsRecord(Base):
    """One fiscal quarter of one stock's earnings — reported or upcoming.

    ``eps_actual`` is ``NULL`` for a quarter that hasn't reported yet (an upcoming
    quarter), set once it has. ``eps_surprise`` / ``eps_surprise_percent`` and
    ``revenue_actual`` are only meaningful for reported quarters; ``revenue_estimate``
    only for the nearest upcoming ones (the source publishes forward revenue just a
    quarter or two out). Everything but the fiscal identity and ``fetched_at`` is
    nullable, since coverage tapers toward the far future.
    """

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
    """All stored quarter rows for ``symbol`` (joined through the ``stocks`` anchor),
    ordered oldest→newest by fiscal period. Empty when nothing is stored for it yet."""
    return list(
        session.execute(
            select(StockQuarterlyEarningsRecord)
            .join(StockRecord, StockQuarterlyEarningsRecord.stock_id == StockRecord.id)
            .where(StockRecord.symbol == symbol)
            .order_by(
                StockQuarterlyEarningsRecord.fiscal_year.asc(),
                StockQuarterlyEarningsRecord.fiscal_quarter.asc(),
            )
        ).scalars()
    )


def delete_quarters_for_stock(session: Session, stock_id: uuid.UUID) -> None:
    """Remove every stored quarter for a stock, so a refresh can rewrite the window
    wholesale (delete-then-insert) rather than diffing rows."""
    session.execute(
        delete(StockQuarterlyEarningsRecord).where(
            StockQuarterlyEarningsRecord.stock_id == stock_id
        )
    )


def stalest_symbols(session: Session, limit: int) -> list[tuple[str, str | None]]:
    """Up to ``limit`` stored ``(symbol, name)`` pairs, stalest-fetched first.

    One entry per stock (grouped over its quarter rows, ordered by the oldest fetch
    stamp among them). Only symbols that already have quarter rows are returned, so a
    refresh walks what's actually cached; never-viewed symbols are filled lazily on
    first access instead.
    """
    rows = session.execute(
        select(StockRecord.symbol, StockRecord.name)
        .join(
            StockQuarterlyEarningsRecord,
            StockQuarterlyEarningsRecord.stock_id == StockRecord.id,
        )
        .group_by(StockRecord.id, StockRecord.symbol, StockRecord.name)
        .order_by(func.min(StockQuarterlyEarningsRecord.fetched_at).asc())
        .limit(limit)
    ).all()
    return [(row.symbol, row.name) for row in rows]
