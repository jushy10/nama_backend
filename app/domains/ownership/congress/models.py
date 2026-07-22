from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
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
from app.domains.listings.anchor.models import StockRecord, get_or_create_stock  # noqa: F401


class StockCongressTradeRecord(Base):
    __tablename__ = "stock_congress_trades"
    __table_args__ = (
        UniqueConstraint(
            "stock_id",
            "member",
            "transaction_date",
            "amount_range",
            "chamber",
            name="uq_congress_stock_member_date_amount_chamber",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    member: Mapped[str] = mapped_column(String(160), nullable=False)
    chamber: Mapped[str] = mapped_column(String(16), nullable=False)
    party: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tx_type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_range: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transaction_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    disclosure_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _activity_date():
    return func.coalesce(
        StockCongressTradeRecord.disclosure_date,
        StockCongressTradeRecord.transaction_date,
    )


def _order_newest_first() -> tuple:
    return (
        _activity_date().desc(),
        StockCongressTradeRecord.disclosure_date.desc(),
        StockCongressTradeRecord.transaction_date.desc(),
        StockCongressTradeRecord.member.asc(),
        StockCongressTradeRecord.id.asc(),
    )


def trades_by_symbol(
    session: Session, symbol: str
) -> list[StockCongressTradeRecord]:
    return list(
        session.execute(
            select(StockCongressTradeRecord)
            .join(StockRecord, StockCongressTradeRecord.stock_id == StockRecord.id)
            .where(StockRecord.ticker == symbol)
            .order_by(*_order_newest_first())
        ).scalars()
    )


def recent_market_trades(
    session: Session, *, since: date | None, limit: int, offset: int
):
    stmt = (
        select(StockCongressTradeRecord, StockRecord.ticker, StockRecord.name)
        .join(StockRecord, StockCongressTradeRecord.stock_id == StockRecord.id)
    )
    if since is not None:
        stmt = stmt.where(_activity_date() >= since)
    stmt = stmt.order_by(*_order_newest_first()).limit(limit).offset(offset)
    return session.execute(stmt).all()


def market_trades_in_window(session: Session, *, since: date | None):
    stmt = select(
        StockCongressTradeRecord, StockRecord.ticker, StockRecord.name
    ).join(StockRecord, StockCongressTradeRecord.stock_id == StockRecord.id)
    if since is not None:
        stmt = stmt.where(_activity_date() >= since)
    stmt = stmt.order_by(*_order_newest_first())
    return session.execute(stmt).all()


def count_recent_market_trades(session: Session, *, since: date | None) -> int:
    stmt = select(func.count()).select_from(StockCongressTradeRecord)
    if since is not None:
        stmt = stmt.where(_activity_date() >= since)
    return int(session.execute(stmt).scalar_one())


def stalest_symbols(
    session: Session, limit: int | None = None
) -> list[tuple[str, str | None]]:
    max_fetched = func.max(StockCongressTradeRecord.fetched_at)
    stmt = (
        select(StockRecord.ticker, StockRecord.name)
        .outerjoin(
            StockCongressTradeRecord,
            StockCongressTradeRecord.stock_id == StockRecord.id,
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
) -> set[tuple[str, date | None, str | None, str]]:
    rows = session.execute(
        select(
            StockCongressTradeRecord.member,
            StockCongressTradeRecord.transaction_date,
            StockCongressTradeRecord.amount_range,
            StockCongressTradeRecord.chamber,
        ).where(StockCongressTradeRecord.stock_id == stock_id)
    ).all()
    return {(r.member, r.transaction_date, r.amount_range, r.chamber) for r in rows}


def touch_fetched_at(session: Session, stock_id: uuid.UUID, now: datetime) -> None:
    session.execute(
        update(StockCongressTradeRecord)
        .where(StockCongressTradeRecord.stock_id == stock_id)
        .values(fetched_at=now)
    )


def prune_to_newest(session: Session, stock_id: uuid.UUID, keep: int) -> None:
    ids = list(
        session.execute(
            select(StockCongressTradeRecord.id)
            .where(StockCongressTradeRecord.stock_id == stock_id)
            .order_by(*_order_newest_first())
        ).scalars()
    )
    surplus = ids[keep:]
    if surplus:
        session.execute(
            delete(StockCongressTradeRecord).where(
                StockCongressTradeRecord.id.in_(surplus)
            )
        )
