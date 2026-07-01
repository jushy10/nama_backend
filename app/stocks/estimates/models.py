"""Database models + queries for the analyst-estimates cache.

The persistence primitives for the estimates slice: the SQLAlchemy models for the two
tables this feature owns — the shared ``stocks`` anchor and ``stock_analyst_estimates``
— plus simple, entity-free query functions over them. The concrete repository
(``db_repository.py``) is the only caller; it maps these rows to and from the
``AnalystEstimates`` entity. Nothing here knows the domain entity — this layer deals
only in rows and columns, so it stays a thin data-access layer.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base


class StockRecord(Base):
    """A stock as stored in the database — the anchor per-feature tables reference.

    ``id`` is a surrogate UUID so child rows have a stable foreign key; ``symbol`` is
    the ticker everything is looked up by (unique); ``name`` is the company display
    name, nullable so a lazily-stored symbol (which arrives with only its ticker)
    still gets a row until a sync fills the name in.
    """

    __tablename__ = "stocks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)


class StockAnalystEstimatesRecord(Base):
    """One stock's current forward consensus — at most one row per stock.

    FY1 (the nearest forward fiscal year) is stored in full; for FY2 (the year after)
    only the EPS and revenue are kept, since downstream the year-2 figures feed only
    the FY1→FY2 growth. ``fetched_at`` stamps the refresh so the cache decorator can
    judge staleness. All estimate columns are nullable: an uncovered symbol still
    gets a (stamped, all-null) row so it isn't re-fetched on every request.
    """

    __tablename__ = "stock_analyst_estimates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    # FY1 — the nearest forward fiscal year, in full.
    fiscal_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    eps_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    num_analysts_eps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    num_analysts_revenue: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # FY2 — only the figures the forward growth needs.
    fiscal_year_fy2: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eps_avg_fy2: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_avg_fy2: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def get_or_create_stock(
    session: Session, symbol: str, name: str | None
) -> StockRecord:
    """Return the ``stocks`` row for ``symbol``, creating it if absent.

    Fills a missing name when one is supplied, but never clobbers a known name with
    ``None`` — so whichever feature first learns the company name sets it, and a later
    nameless write (e.g. an estimates refresh) leaves it intact. The new row is flushed
    so its ``id`` is available for a child row in the same unit of work.
    """
    stock = session.execute(
        select(StockRecord).where(StockRecord.symbol == symbol)
    ).scalar_one_or_none()
    if stock is None:
        stock = StockRecord(symbol=symbol, name=name)
        session.add(stock)
        session.flush()  # assign stock.id before a child row references it
    elif name and not stock.name:
        stock.name = name
    return stock


def estimates_by_symbol(
    session: Session, symbol: str
) -> StockAnalystEstimatesRecord | None:
    """The estimates row for ``symbol`` (joined through the ``stocks`` anchor), or
    ``None`` when nothing is stored for it yet."""
    return session.execute(
        select(StockAnalystEstimatesRecord)
        .join(StockRecord, StockAnalystEstimatesRecord.stock_id == StockRecord.id)
        .where(StockRecord.symbol == symbol)
    ).scalar_one_or_none()


def estimates_by_stock_id(
    session: Session, stock_id: uuid.UUID
) -> StockAnalystEstimatesRecord | None:
    """The estimates row hanging off a given ``stocks.id``, or ``None``."""
    return session.execute(
        select(StockAnalystEstimatesRecord).where(
            StockAnalystEstimatesRecord.stock_id == stock_id
        )
    ).scalar_one_or_none()


def stalest_symbols(session: Session, limit: int) -> list[tuple[str, str | None]]:
    """Up to ``limit`` stored ``(symbol, name)`` pairs, oldest-fetched first.

    Only symbols that already have an estimates row are returned (the join enforces
    it), so a refresh walks what's actually cached; never-viewed symbols are filled
    lazily on first access instead.
    """
    rows = session.execute(
        select(StockRecord.symbol, StockRecord.name)
        .join(
            StockAnalystEstimatesRecord,
            StockAnalystEstimatesRecord.stock_id == StockRecord.id,
        )
        .order_by(StockAnalystEstimatesRecord.fetched_at.asc())
        .limit(limit)
    ).all()
    return [(row.symbol, row.name) for row in rows]
