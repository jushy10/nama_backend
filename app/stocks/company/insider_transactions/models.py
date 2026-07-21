from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
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
    update,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base

# The shared ``stocks`` anchor + its get-or-create helper, re-exported so the repository reaches
# them as ``models.StockRecord`` / ``models.get_or_create_stock``.
from app.stocks.catalog.anchor.models import StockRecord, get_or_create_stock  # noqa: F401


class StockInsiderTransactionRecord(Base):
    __tablename__ = "stock_insider_transactions"
    __table_args__ = (
        UniqueConstraint(
            "stock_id",
            "accession_number",
            "line_index",
            name="uq_insider_txn_stock_acc_line",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    filing_date: Mapped[date] = mapped_column(Date, nullable=False)
    transaction_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Free-text fields sized generously (255) — real Form 4 values are far shorter, but a
    # bank/REIT preferred-stock ``security_title`` can run ~145 chars, and the adapter also clips
    # to this width so an outlier can never overflow the column and (silently, via the swallowed
    # cache write) poison the stock's cache on Postgres. See _MAX_TEXT_LEN in the SEC adapter.
    insider_name: Mapped[str] = mapped_column(String(255), nullable=False)
    officer_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_director: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_officer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_ten_percent_owner: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    security_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transaction_code: Mapped[str] = mapped_column(String(2), nullable=False)
    acquired_disposed: Mapped[str | None] = mapped_column(String(1), nullable=True)
    shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    shares_owned_following: Mapped[float | None] = mapped_column(Float, nullable=True)
    accession_number: Mapped[str] = mapped_column(String(25), nullable=False)
    line_index: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _order_newest_first() -> tuple:
    return (
        func.coalesce(
            StockInsiderTransactionRecord.transaction_date,
            StockInsiderTransactionRecord.filing_date,
        ).desc(),
        StockInsiderTransactionRecord.filing_date.desc(),
        StockInsiderTransactionRecord.accession_number.desc(),
        StockInsiderTransactionRecord.line_index.asc(),
    )


def transactions_by_symbol(
    session: Session, symbol: str
) -> list[StockInsiderTransactionRecord]:
    return list(
        session.execute(
            select(StockInsiderTransactionRecord)
            .join(
                StockRecord,
                StockInsiderTransactionRecord.stock_id == StockRecord.id,
            )
            .where(StockRecord.ticker == symbol)
            .order_by(*_order_newest_first())
        ).scalars()
    )


def stalest_symbols(
    session: Session, limit: int | None = None
) -> list[tuple[str, str | None]]:
    max_fetched = func.max(StockInsiderTransactionRecord.fetched_at)
    stmt = (
        select(StockRecord.ticker, StockRecord.name)
        .outerjoin(
            StockInsiderTransactionRecord,
            StockInsiderTransactionRecord.stock_id == StockRecord.id,
        )
        .group_by(StockRecord.id, StockRecord.ticker, StockRecord.name)
        # un-cached (NULL stamp) first, then least-recently-refreshed — portable NULLs-first.
        .order_by(max_fetched.is_(None).desc(), max_fetched.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(row.ticker, row.name) for row in session.execute(stmt).all()]


def existing_keys_for_stock(
    session: Session, stock_id: uuid.UUID
) -> set[tuple[str, int]]:
    rows = session.execute(
        select(
            StockInsiderTransactionRecord.accession_number,
            StockInsiderTransactionRecord.line_index,
        ).where(StockInsiderTransactionRecord.stock_id == stock_id)
    ).all()
    return {(row.accession_number, row.line_index) for row in rows}


def touch_fetched_at(
    session: Session, stock_id: uuid.UUID, now: datetime
) -> None:
    session.execute(
        update(StockInsiderTransactionRecord)
        .where(StockInsiderTransactionRecord.stock_id == stock_id)
        .values(fetched_at=now)
    )


def prune_to_newest(session: Session, stock_id: uuid.UUID, keep: int) -> None:
    ids = list(
        session.execute(
            select(StockInsiderTransactionRecord.id)
            .where(StockInsiderTransactionRecord.stock_id == stock_id)
            .order_by(*_order_newest_first())
        ).scalars()
    )
    surplus = ids[keep:]
    if surplus:
        session.execute(
            delete(StockInsiderTransactionRecord).where(
                StockInsiderTransactionRecord.id.in_(surplus)
            )
        )
