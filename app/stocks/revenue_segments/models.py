"""Database model + queries for the revenue-segments cache.

The persistence primitives for the slice: the SQLAlchemy model for the
``stock_revenue_segments`` table this feature owns, plus simple, entity-free query functions
over it. The shared ``stocks`` anchor these rows hang off of lives in its own slice,
``app/stocks/stocks/models.py`` (owned by no single feature), and is imported here. The
concrete repository (``db_repository.py``) is the only caller; it maps these rows to and from
the ``RevenueSegment`` entity. Nothing here knows the domain entity — this layer deals only in
rows and columns, so it stays a thin data-access layer.

A time series: many rows per stock, one per ``(fiscal_year, axis, member)`` figure, keyed
unique on those. Like the recommendations/news tables a refresh *merges* — it replaces exactly
the fiscal years the filing restated and keeps earlier ones — so rows for one stock can carry
different ``fetched_at`` stamps; a stock's last refresh is the *max* stamp over its rows. The
accumulated history is **pruned** to the newest ``keep`` fiscal years per stock
(``prune_to_newest_years``) so it stays bounded.
"""

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
from app.stocks.stocks.models import StockRecord, get_or_create_stock  # noqa: F401


class StockRevenueSegmentRecord(Base):
    """One revenue figure for one stock, fiscal year, axis, and member.

    ``axis`` is a ``SegmentAxis`` slug (``business_segment`` / ``product`` / ``geography``);
    ``member`` is the filer's raw XBRL member local-name (``GoogleCloudMember``) — the pair
    identifies the disaggregation cut, and with ``fiscal_year`` forms the row's unique key.
    ``value`` is revenue in the filing's reporting currency (raw, typically USD). ``period_end``
    (the fiscal year end) is nullable — the label is derivable from the figure without it.
    """

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
    """All stored segment rows for ``symbol`` (joined through the ``stocks`` anchor), newest
    fiscal year first. Empty when nothing is stored for it yet."""
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
    """Remove a stock's rows for exactly ``fiscal_years``, so a refresh can re-insert the years
    the filing restated while leaving earlier stored years intact (the merge). Replacing whole
    years (not individual members) means a segment the filer renamed or dropped doesn't linger."""
    if not fiscal_years:
        return
    session.execute(
        delete(StockRevenueSegmentRecord).where(
            StockRevenueSegmentRecord.stock_id == stock_id,
            StockRevenueSegmentRecord.fiscal_year.in_(list(fiscal_years)),
        )
    )


def prune_to_newest_years(session: Session, stock_id: uuid.UUID, keep: int) -> None:
    """Delete all but the ``keep`` newest fiscal years for ``stock_id``, so the accumulated
    history stays bounded. Fetches the distinct years, finds the surplus older ones, and deletes
    their rows — portable across SQLite/Postgres and cheap (a stock holds only a handful of
    years). Pruning by *year* (not row count) keeps every axis/member of a kept year together."""
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
    """``(symbol, name)`` pairs from the ``stocks`` anchor, most in need of a refresh first.

    A **LEFT JOIN**, so every anchor stock is included — even one with no segment rows yet — and
    the sync both *seeds* new coverage and renews stale rows. Cached stocks are ordered by the
    *newest* fetch stamp among their rows (the merge keeps old stamps on old years forever, so
    the min would pin a long-cached stock permanently stale; the max is when it was last actually
    refreshed). Ordering is **un-cached first**: a never-fetched stock has a NULL max stamp and
    sorts ahead of any cached stock. ``limit`` caps the batch; ``None`` (the default) returns
    every stock, so one sweep can seed the whole anchor. Lazy fill on first access still covers a
    symbol between sweeps.
    """
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
