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
from app.domains.listings.anchor.models import StockRecord, get_or_create_stock  # noqa: F401


class StockAnnualEarningsRecord(Base):
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
    # The reported year's actual EPS on the analyst-consensus basis (sum of its four
    # quarterly "Reported EPS" values) — comparable with eps_estimate, unlike the
    # GAAP-diluted eps_actual. Best-effort, reported years only.
    eps_actual_consensus: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The reported year's free-cash-flow / operating-cash-flow per share (trading
    # currency), from the cash-flow statement over the year's diluted average shares.
    # Persisted per-year so the merge-preserving sync can carry them forward when Yahoo
    # blocks the (hard-gated) cash-flow fetch. Best-effort, reported years only (0027).
    fcf_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    ocf_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def years_by_symbol(session: Session, symbol: str) -> list[StockAnnualEarningsRecord]:
    return list(
        session.execute(
            select(StockAnnualEarningsRecord)
            .join(StockRecord, StockAnnualEarningsRecord.stock_id == StockRecord.id)
            .where(StockRecord.ticker == symbol)
            .order_by(StockAnnualEarningsRecord.fiscal_year.asc())
        ).scalars()
    )


def delete_years_for_stock(session: Session, stock_id: uuid.UUID) -> None:
    session.execute(
        delete(StockAnnualEarningsRecord).where(
            StockAnnualEarningsRecord.stock_id == stock_id
        )
    )


def stalest_symbols(
    session: Session, limit: int | None = None
) -> list[tuple[str, str | None]]:
    min_fetched = func.min(StockAnnualEarningsRecord.fetched_at)
    stmt = (
        select(StockRecord.ticker, StockRecord.name)
        .outerjoin(
            StockAnnualEarningsRecord,
            StockAnnualEarningsRecord.stock_id == StockRecord.id,
        )
        .group_by(StockRecord.id, StockRecord.ticker, StockRecord.name)
        # un-cached (NULL stamp) first, then stalest cached — portable NULLs-first ordering.
        .order_by(min_fetched.is_(None).desc(), min_fetched.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(row.ticker, row.name) for row in session.execute(stmt).all()]
