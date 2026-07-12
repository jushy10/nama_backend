"""Database models + queries for the institutional-ownership cache.

The persistence primitives for the slice: two SQLAlchemy models this feature owns —
``stock_institutional_holders`` (a time series: many rows per stock, one per holder per reported
quarter) and ``stock_ownership_summary`` (one row per stock, the current breakdown) — plus simple,
entity-free query functions over them. The shared ``stocks`` anchor these rows hang off of lives in
its own slice, ``app/stocks/stocks/models.py`` (owned by no single feature), and is imported here.
The concrete repository (``db_repository.py``) is the only caller; it maps these rows to and from
the ``InstitutionalHolder`` / ``OwnershipBreakdown`` entities. Nothing here knows the domain entity
— this layer deals only in rows and columns, so it stays a thin data-access layer.

The holders table is keyed unique on ``(stock_id, holder_type, holder, date_reported)`` — one row
per holder per reported quarter. Like the news table a refresh *merges* — it replaces the snapshots
it re-served (a reported quarter's holdings are a frozen fact once filed) and keeps earlier ones —
so the store accumulates a longer history than the source serves at once, **pruned** to the newest
``keep`` rows per stock. ``fetched_at`` is a cache-bookkeeping stamp; a stock's last refresh is the
*max* stamp over its rows.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Iterable

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


class StockInstitutionalHolderRecord(Base):
    """One institutional/mutual-fund holder's stake in a stock as of one reported 13F quarter.

    ``holder_type`` (``institution`` / ``mutual_fund``) + ``holder`` + ``date_reported`` form the
    row's unique key alongside ``stock_id``. ``shares`` / ``value`` / ``pct_held`` / ``pct_change``
    are all nullable — the source is best-effort — so the derived share/value change is best-effort
    too. ``pct_held`` / ``pct_change`` are percent."""

    __tablename__ = "stock_institutional_holders"
    __table_args__ = (
        UniqueConstraint(
            "stock_id",
            "holder_type",
            "holder",
            "date_reported",
            name="uq_inst_holder_stock_type_holder_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    # Holder names run long (asset-manager legal names); sized generously like the insider slice.
    holder: Mapped[str] = mapped_column(String(255), nullable=False)
    holder_type: Mapped[str] = mapped_column(String(16), nullable=False)
    date_reported: Mapped[date] = mapped_column(Date, nullable=False)
    shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    pct_held: Mapped[float | None] = mapped_column(Float, nullable=True)
    pct_change: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StockOwnershipSummaryRecord(Base):
    """The current ownership breakdown for a stock — one row per stock (overwritten each refresh).

    The headline "institutions own X% of the float" summary, distinct from the per-holder feed:
    Yahoo publishes only a single current snapshot, so this is not a time series. All percent
    fields nullable (best-effort)."""

    __tablename__ = "stock_ownership_summary"
    __table_args__ = (
        UniqueConstraint("stock_id", name="uq_ownership_summary_stock"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    institutions_pct_held: Mapped[float | None] = mapped_column(Float, nullable=True)
    insiders_pct_held: Mapped[float | None] = mapped_column(Float, nullable=True)
    institutions_float_pct_held: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    institutions_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _order_newest_first() -> tuple:
    """The canonical serving/pruning order: most recently reported quarter first, largest position
    (by value) first within a quarter, then a stable tiebreak so the order is deterministic. Kept
    identical to the adapter's own sort so a live-served and a cache-served response match. A NULL
    value is coalesced to ``-1`` (sorting last) rather than using ``NULLS LAST`` — SQLite and
    Postgres disagree on NULL ordering under ``DESC``, and the sentinel is portable and matches the
    adapter's ``_holder_sort_key``."""
    return (
        StockInstitutionalHolderRecord.date_reported.desc(),
        func.coalesce(StockInstitutionalHolderRecord.value, -1.0).desc(),
        StockInstitutionalHolderRecord.holder.asc(),
    )


def holders_by_symbol(
    session: Session, symbol: str
) -> list[StockInstitutionalHolderRecord]:
    """All stored holder rows for ``symbol`` (joined through the ``stocks`` anchor), newest
    reported quarter first. Empty when nothing is stored for it yet."""
    return list(
        session.execute(
            select(StockInstitutionalHolderRecord)
            .join(
                StockRecord,
                StockInstitutionalHolderRecord.stock_id == StockRecord.id,
            )
            .where(StockRecord.ticker == symbol)
            .order_by(*_order_newest_first())
        ).scalars()
    )


def summary_by_symbol(
    session: Session, symbol: str
) -> StockOwnershipSummaryRecord | None:
    """The stored ownership-breakdown row for ``symbol``, or ``None`` when none is stored."""
    return session.execute(
        select(StockOwnershipSummaryRecord)
        .join(StockRecord, StockOwnershipSummaryRecord.stock_id == StockRecord.id)
        .where(StockRecord.ticker == symbol)
    ).scalar_one_or_none()


def summary_for_stock(
    session: Session, stock_id: uuid.UUID
) -> StockOwnershipSummaryRecord | None:
    """The stored breakdown row for ``stock_id`` (by surrogate id), for the overwrite upsert."""
    return session.execute(
        select(StockOwnershipSummaryRecord).where(
            StockOwnershipSummaryRecord.stock_id == stock_id
        )
    ).scalar_one_or_none()


def delete_holder_snapshots(
    session: Session,
    stock_id: uuid.UUID,
    snapshots: Iterable[tuple[str, date]],
) -> None:
    """Remove a stock's holder rows for exactly the ``(holder_type, date_reported)`` snapshots the
    refresh re-served, so they can be re-inserted fresh while earlier reported quarters are left
    intact (the merge). A fetch brings the whole top-N for each snapshot, so replacing the snapshot
    wholesale also drops a holder that fell out of the current top-N — the correct behaviour. Few
    snapshots per fetch (≤2), so a delete each stays cheap and portable across SQLite/Postgres."""
    for holder_type, date_reported in set(snapshots):
        session.execute(
            delete(StockInstitutionalHolderRecord).where(
                StockInstitutionalHolderRecord.stock_id == stock_id,
                StockInstitutionalHolderRecord.holder_type == holder_type,
                StockInstitutionalHolderRecord.date_reported == date_reported,
            )
        )


def prune_to_newest(session: Session, stock_id: uuid.UUID, keep: int) -> None:
    """Delete all but the ``keep`` newest holder rows for ``stock_id`` (by the serving order) so the
    accumulated multi-quarter history stays bounded. Selects the row ids in order and deletes the
    surplus tail — portable across SQLite/Postgres."""
    ids = list(
        session.execute(
            select(StockInstitutionalHolderRecord.id)
            .where(StockInstitutionalHolderRecord.stock_id == stock_id)
            .order_by(*_order_newest_first())
        ).scalars()
    )
    surplus = ids[keep:]
    if surplus:
        session.execute(
            delete(StockInstitutionalHolderRecord).where(
                StockInstitutionalHolderRecord.id.in_(surplus)
            )
        )


def stalest_symbols(
    session: Session, limit: int | None = None
) -> list[tuple[str, str | None]]:
    """``(symbol, name)`` pairs from the ``stocks`` anchor, most in need of a refresh first.

    A **LEFT JOIN**, so every anchor stock is included — even one with no holder rows yet — and the
    sync both *seeds* new coverage and renews stale rows. Cached stocks are ordered by the *newest*
    fetch stamp among their rows (the merge keeps old quarters' stamps forever, so the min would pin
    a long-cached stock permanently stale; the max is when it was last actually refreshed).
    Un-cached first: a never-fetched stock has a NULL max stamp and sorts ahead of any cached stock.
    ``limit`` caps the batch; ``None`` (the default) returns every stock."""
    max_fetched = func.max(StockInstitutionalHolderRecord.fetched_at)
    stmt = (
        select(StockRecord.ticker, StockRecord.name)
        .outerjoin(
            StockInstitutionalHolderRecord,
            StockInstitutionalHolderRecord.stock_id == StockRecord.id,
        )
        .group_by(StockRecord.id, StockRecord.ticker, StockRecord.name)
        .order_by(max_fetched.is_(None).desc(), max_fetched.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(row.ticker, row.name) for row in session.execute(stmt).all()]
