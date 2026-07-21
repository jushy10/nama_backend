from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    delete,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base


class EtfRecord(Base):
    __tablename__ = "etfs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ticker: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    net_assets: Mapped[float | None] = mapped_column(Float, nullable=True)
    expense_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    screened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # --- The per-fund profile (filled by the enrichment pass; see the class docstring) ---
    fund_family: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dividend_yield: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    nav: Mapped[float | None] = mapped_column(Float, nullable=True)
    profile_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class EtfSectorWeightingRecord(Base):
    __tablename__ = "etf_sector_weightings"
    __table_args__ = (
        UniqueConstraint(
            "etf_id", "sector", name="uq_etf_sector_weightings_etf_sector"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    etf_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("etfs.id", ondelete="CASCADE"), nullable=False
    )
    sector: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EtfTopHoldingRecord(Base):
    __tablename__ = "etf_top_holdings"
    __table_args__ = (
        UniqueConstraint(
            "etf_id", "position", name="uq_etf_top_holdings_etf_position"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    etf_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("etfs.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(16), nullable=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def get_or_create_etf(session: Session, ticker: str, name: str | None) -> EtfRecord:
    etf = session.execute(
        select(EtfRecord).where(EtfRecord.ticker == ticker)
    ).scalar_one_or_none()
    if etf is None:
        etf = EtfRecord(ticker=ticker, name=name)
        session.add(etf)
        session.flush()
    elif name and not etf.name:
        etf.name = name
    return etf


def profile_refresh_targets(
    session: Session, limit: int | None = None
) -> list[str]:
    stmt = (
        select(EtfRecord.ticker)
        .order_by(
            EtfRecord.profile_fetched_at.is_(None).desc(),
            EtfRecord.profile_fetched_at.asc(),
            EtfRecord.ticker.asc(),
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars().all())


def sector_weightings_for_etf(
    session: Session, ticker: str
) -> list[EtfSectorWeightingRecord]:
    return list(
        session.execute(
            select(EtfSectorWeightingRecord)
            .join(EtfRecord, EtfSectorWeightingRecord.etf_id == EtfRecord.id)
            .where(EtfRecord.ticker == ticker)
            .order_by(EtfSectorWeightingRecord.weight.desc())
        ).scalars()
    )


def top_holdings_for_etf(
    session: Session, ticker: str
) -> list[EtfTopHoldingRecord]:
    return list(
        session.execute(
            select(EtfTopHoldingRecord)
            .join(EtfRecord, EtfTopHoldingRecord.etf_id == EtfRecord.id)
            .where(EtfRecord.ticker == ticker)
            .order_by(EtfTopHoldingRecord.position.asc())
        ).scalars()
    )


def delete_sector_weightings_for_etf(session: Session, etf_id: uuid.UUID) -> None:
    session.execute(
        delete(EtfSectorWeightingRecord).where(
            EtfSectorWeightingRecord.etf_id == etf_id
        )
    )


def delete_top_holdings_for_etf(session: Session, etf_id: uuid.UUID) -> None:
    session.execute(
        delete(EtfTopHoldingRecord).where(EtfTopHoldingRecord.etf_id == etf_id)
    )
