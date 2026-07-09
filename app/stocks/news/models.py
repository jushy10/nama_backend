"""Database model + queries for the news cache.

The persistence primitives for the slice: the SQLAlchemy model for the ``stock_news``
table this feature owns, plus simple, entity-free query functions over it. The shared
``stocks`` anchor these rows hang off of lives in its own slice,
``app/stocks/stocks/models.py`` (owned by no single feature), and is imported here. The
concrete repository (``db_repository.py``) is the only caller; it maps these rows to and
from the ``NewsArticle`` entity. Nothing here knows the domain entity — this layer deals
only in rows and columns, so it stays a thin data-access layer.

A time series: many rows per stock, one per article, keyed unique on
``(stock_id, article_id)`` (the source's stable article id). Like the recommendations
table a refresh *merges* — it replaces the articles the source served and keeps earlier
ones — so rows for one stock can carry different ``fetched_at`` stamps; a stock's last
refresh is the *max* stamp over its rows. Unlike recommendations the feed is **pruned**
to the newest ``keep`` articles per stock (``prune_to_newest``), so the higher-volume
news history stays bounded.
"""

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
from app.stocks.stocks.models import StockRecord, get_or_create_stock  # noqa: F401


class StockNewsRecord(Base):
    """One news article about one stock.

    ``article_id`` is the source's stable id (unique per stock), the identity a refresh
    merges on. ``title`` and ``published_at`` are always present (a row with neither is
    never stored); ``publisher`` / ``link`` / ``summary`` / ``content_type`` /
    ``thumbnail_url`` are best-effort and nullable. ``link``, ``summary`` and
    ``thumbnail_url`` are ``Text`` (URLs and blurbs have no useful length bound).
    """

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
    """All stored article rows for ``symbol`` (joined through the ``stocks`` anchor),
    ordered newest→oldest by publish time. Empty when nothing is stored for it yet."""
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
    """Remove a stock's rows for exactly ``article_ids``, so a refresh can re-insert the
    articles the source served while leaving earlier stored ones intact (the merge)."""
    if not article_ids:
        return
    session.execute(
        delete(StockNewsRecord).where(
            StockNewsRecord.stock_id == stock_id,
            StockNewsRecord.article_id.in_(article_ids),
        )
    )


def prune_to_newest(session: Session, stock_id: uuid.UUID, keep: int) -> None:
    """Delete all but the ``keep`` newest articles for ``stock_id`` (by publish time),
    so the accumulated feed stays bounded. Fetches the surplus ids and deletes them —
    portable across SQLite/Postgres (a ``LIMIT/OFFSET`` delete isn't), and cheap because
    a stock holds at most ``keep`` + one fetch's worth of rows at prune time."""
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
    """``(symbol, name)`` pairs from the ``stocks`` anchor, most in need of a refresh first.

    A **LEFT JOIN**, so every anchor stock is included — even one with no article rows yet — and
    the sync both *seeds* new coverage and renews stale rows. Cached stocks are ordered by the
    *newest* fetch stamp among their rows (the merge keeps old stamps on old articles forever, so
    the min would pin a long-cached stock permanently stale; the max is when it was last actually
    refreshed). Ordering is **un-cached first**: a never-fetched stock has a NULL max stamp and
    sorts ahead of any cached stock. ``limit`` caps the batch; ``None`` (the default) returns
    every stock, so one sweep can seed the whole anchor. Lazy fill on first access still covers a
    symbol between sweeps.
    """
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
