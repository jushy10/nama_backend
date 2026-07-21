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
from app.stocks.catalog.anchor.models import StockRecord, get_or_create_stock  # noqa: F401


class StockInstitutionalHolderRecord(Base):
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
    return (
        StockInstitutionalHolderRecord.date_reported.desc(),
        func.coalesce(StockInstitutionalHolderRecord.value, -1.0).desc(),
        StockInstitutionalHolderRecord.holder.asc(),
    )


def holders_by_symbol(
    session: Session, symbol: str
) -> list[StockInstitutionalHolderRecord]:
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
    return session.execute(
        select(StockOwnershipSummaryRecord)
        .join(StockRecord, StockOwnershipSummaryRecord.stock_id == StockRecord.id)
        .where(StockRecord.ticker == symbol)
    ).scalar_one_or_none()


def summary_for_stock(
    session: Session, stock_id: uuid.UUID
) -> StockOwnershipSummaryRecord | None:
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
    for holder_type, date_reported in set(snapshots):
        session.execute(
            delete(StockInstitutionalHolderRecord).where(
                StockInstitutionalHolderRecord.stock_id == stock_id,
                StockInstitutionalHolderRecord.holder_type == holder_type,
                StockInstitutionalHolderRecord.date_reported == date_reported,
            )
        )


def prune_to_newest(session: Session, stock_id: uuid.UUID, keep: int) -> None:
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
