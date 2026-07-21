from __future__ import annotations

import uuid
from datetime import datetime
from typing import Sequence

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
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
from app.stocks.catalog.anchor.models import StockRecord, get_or_create_stock  # noqa: F401


class StockNewsRecord(Base):
    __tablename__ = "stock_news"
    __table_args__ = (
        UniqueConstraint(
            "stock_id",
            "article_id",
            name="uq_stock_news_stock_article",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    article_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    publisher: Mapped[str | None] = mapped_column(String(128), nullable=True)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def articles_by_symbol(session: Session, symbol: str) -> list[StockNewsRecord]:
    return list(
        session.execute(
            select(StockNewsRecord)
            .join(StockRecord, StockNewsRecord.stock_id == StockRecord.id)
            .where(StockRecord.ticker == symbol)
            .order_by(
                StockNewsRecord.published_at.desc(),
                StockNewsRecord.article_id,
            )
        ).scalars()
    )


def delete_articles_for_ids(
    session: Session, stock_id: uuid.UUID, article_ids: Sequence[str]
) -> None:
    if not article_ids:
        return
    session.execute(
        delete(StockNewsRecord).where(
            StockNewsRecord.stock_id == stock_id,
            StockNewsRecord.article_id.in_(article_ids),
        )
    )


def prune_to_newest(session: Session, stock_id: uuid.UUID, keep: int) -> None:
    ids = list(
        session.execute(
            select(StockNewsRecord.id)
            .where(StockNewsRecord.stock_id == stock_id)
            .order_by(
                StockNewsRecord.published_at.desc(),
                StockNewsRecord.article_id,
            )
        ).scalars()
    )
    surplus = ids[keep:]
    if surplus:
        session.execute(
            delete(StockNewsRecord).where(StockNewsRecord.id.in_(surplus))
        )


def stalest_symbols(
    session: Session, limit: int | None = None
) -> list[tuple[str, str | None]]:
    max_fetched = func.max(StockNewsRecord.fetched_at)
    stmt = (
        select(StockRecord.ticker, StockRecord.name)
        .outerjoin(StockNewsRecord, StockNewsRecord.stock_id == StockRecord.id)
        .group_by(StockRecord.id, StockRecord.ticker, StockRecord.name)
        # un-cached (NULL stamp) first, then least-recently-refreshed — portable NULLs-first.
        .order_by(max_fetched.is_(None).desc(), max_fetched.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(row.ticker, row.name) for row in session.execute(stmt).all()]
