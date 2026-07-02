"""Database model + queries for the annual-earnings cache.

The persistence primitives for the slice: the SQLAlchemy model for the
``stock_annual_earnings`` table this feature owns, plus simple, entity-free query
functions over it. The shared ``stocks`` anchor these rows hang off of lives in its own
slice, ``app/stocks/stocks/models.py`` (owned by no single feature), and is imported here.
The concrete repository (``db_repository.py``) is the only caller; it maps these rows to
and from the ``AnnualEarnings`` entity. Nothing here knows the domain entity — this layer
deals only in rows and columns, so it stays a thin data-access layer.

Like ``stock_quarterly_earnings``, this is a time series: many rows per stock, one per
fiscal year, keyed unique on ``(stock_id, fiscal_year)``. A refresh rewrites a stock's
whole window at once (delete-then-insert), so every row for a stock shares one
``fetched_at``.
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


class StockAnnualEarningsRecord(Base):
    """One fiscal year of one stock's earnings — reported or upcoming.

    ``eps_actual`` is ``NULL`` for a year that hasn't reported yet (an upcoming year), set
    once it has. ``revenue_actual`` / ``net_income`` are only meaningful for reported years;
    ``revenue_estimate`` / ``eps_estimate`` for the upcoming ones (the source publishes
    forward consensus just a year or two out). Everything but the fiscal identity and
    ``fetched_at`` is nullable, since coverage tapers toward the far future.
    """

    __tablename__ = "stock_annual_earnings"
    __table_args__ = (
        UniqueConstraint(
            "stock_id",
            "fiscal_year",
            name="uq_annual_earnings_stock_year",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    eps_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_income: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def years_by_symbol(session: Session, symbol: str) -> list[StockAnnualEarningsRecord]:
    """All stored year rows for ``symbol`` (joined through the ``stocks`` anchor), ordered
    oldest→newest by fiscal year. Empty when nothing is stored for it yet."""
    return list(
        session.execute(
            select(StockAnnualEarningsRecord)
            .join(StockRecord, StockAnnualEarningsRecord.stock_id == StockRecord.id)
            .where(StockRecord.symbol == symbol)
            .order_by(StockAnnualEarningsRecord.fiscal_year.asc())
        ).scalars()
    )


def years_by_symbols(
    session: Session, symbols: list[str]
) -> list[tuple[str, StockAnnualEarningsRecord]]:
    """All stored year rows for the given symbols in one query, as ``(symbol, row)``
    pairs ordered by symbol then ascending fiscal year. Symbols with nothing stored
    simply contribute no pairs. The batch companion to ``years_by_symbol`` — the
    growth screener reads a whole universe, so per-symbol queries would be far too
    many round-trips."""
    if not symbols:
        return []
    rows = session.execute(
        select(StockRecord.symbol, StockAnnualEarningsRecord)
        .join(StockRecord, StockAnnualEarningsRecord.stock_id == StockRecord.id)
        .where(StockRecord.symbol.in_(symbols))
        .order_by(StockRecord.symbol.asc(), StockAnnualEarningsRecord.fiscal_year.asc())
    ).all()
    return [(row.symbol, row[1]) for row in rows]


def delete_years_for_stock(session: Session, stock_id: uuid.UUID) -> None:
    """Remove every stored year for a stock, so a refresh can rewrite the window wholesale
    (delete-then-insert) rather than diffing rows."""
    session.execute(
        delete(StockAnnualEarningsRecord).where(
            StockAnnualEarningsRecord.stock_id == stock_id
        )
    )


def stored_symbols_among(session: Session, symbols: list[str]) -> set[str]:
    """The subset of ``symbols`` that already has at least one stored year row. One
    query, so the sync can split a whole constituent list into seeds vs. stored."""
    if not symbols:
        return set()
    rows = session.execute(
        select(StockRecord.symbol)
        .join(
            StockAnnualEarningsRecord,
            StockAnnualEarningsRecord.stock_id == StockRecord.id,
        )
        .where(StockRecord.symbol.in_(symbols))
        .distinct()
    ).all()
    return {row.symbol for row in rows}


def stalest_symbols(session: Session, limit: int) -> list[tuple[str, str | None]]:
    """Up to ``limit`` stored ``(symbol, name)`` pairs, stalest-fetched first.

    One entry per stock (grouped over its year rows, ordered by the oldest fetch stamp among
    them). Only symbols that already have year rows are returned, so a refresh walks what's
    actually cached; never-viewed symbols are filled lazily on first access instead.
    """
    rows = session.execute(
        select(StockRecord.symbol, StockRecord.name)
        .join(
            StockAnnualEarningsRecord,
            StockAnnualEarningsRecord.stock_id == StockRecord.id,
        )
        .group_by(StockRecord.id, StockRecord.symbol, StockRecord.name)
        .order_by(func.min(StockAnnualEarningsRecord.fetched_at).asc())
        .limit(limit)
    ).all()
    return [(row.symbol, row.name) for row in rows]
