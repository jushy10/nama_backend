"""Database models + queries for the analyst-coverage cache.

The persistence primitives for the slice: the SQLAlchemy models for the two tables this
feature owns — ``stock_analyst_trends`` (the monthly buy/hold/sell series, which also
carries the current consensus price target on its latest row) and its sibling
``stock_analyst_rating_changes`` (the discrete upgrade/downgrade events) — plus simple,
entity-free query functions over them. The shared ``stocks`` anchor these rows hang off of
lives in its own slice, ``app/stocks/stocks/models.py`` (owned by no single feature), and
is imported here. The concrete repository (``db_repository.py``) is the only caller; it maps
these rows to and from the domain entities. Nothing here knows the entities — this layer
deals only in rows and columns, so it stays a thin data-access layer.

Both are time series: many rows per stock. ``stock_analyst_trends`` is keyed unique on
``(stock_id, period)`` (one row per monthly snapshot); ``stock_analyst_rating_changes`` on
``(stock_id, firm, published_at)`` (one row per firm action). Unlike the earnings tables,
a refresh *merges* — trends replace the months the source served and keep earlier ones,
rating changes are insert-only (each event is a frozen fact) — so rows for one stock can
carry different ``fetched_at`` stamps; a stock's last refresh is the *max* stamp over its
rows. (The tables keep the ``uq_recommendation_trends_*`` constraint name from before the
0023 rename — a cosmetic legacy label, not worth a table rebuild to change.)
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

# The shared ``stocks`` anchor + its get-or-create helper, re-exported so the repository
# reaches them as ``models.StockRecord`` / ``models.get_or_create_stock``.
from app.stocks.stocks.models import StockRecord, get_or_create_stock  # noqa: F401


class StockRecommendationTrendRecord(Base):
    """One monthly snapshot of one stock's analyst buy/hold/sell split.

    The five counts are how many sell-side analysts held each stance that month; all are
    non-null (an uncovered bucket is 0, and a symbol with no coverage at all simply has
    no row). ``period`` is the first day of the month the snapshot covers.

    The four ``target_*`` columns carry the current consensus price target (mean/high/low/
    median). They are a single *current* snapshot, not a per-month history, so they are set
    only on the stock's **latest** row and left null on older ones; a refresh rewrites the
    latest month's row (and thus its targets) each run. All nullable — a stock with no
    price-target coverage simply carries nulls.
    """

    __tablename__ = "stock_analyst_trends"
    __table_args__ = (
        UniqueConstraint(
            "stock_id",
            "period",
            name="uq_recommendation_trends_stock_period",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    period: Mapped[date] = mapped_column(Date, nullable=False)
    strong_buy: Mapped[int] = mapped_column(Integer, nullable=False)
    buy: Mapped[int] = mapped_column(Integer, nullable=False)
    hold: Mapped[int] = mapped_column(Integer, nullable=False)
    sell: Mapped[int] = mapped_column(Integer, nullable=False)
    strong_sell: Mapped[int] = mapped_column(Integer, nullable=False)
    # Consensus 12-month price target — set on the latest row only, nullable elsewhere.
    target_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StockAnalystRatingChangeRecord(Base):
    """One published sell-side rating action on a stock (an upgrade/downgrade event).

    Keyed unique on ``(stock_id, firm, published_at)`` — one row per firm action per day.
    ``firm`` and ``published_at`` are non-null (they form the identity); the grades, action
    label, and price targets are nullable (an initiation has no prior grade, a rating-only
    note no target). Insert-only: each event is a frozen fact, so a refresh only adds newly
    published rows and never rewrites stored ones — the table accumulates a longer history
    than Yahoo serves at once, the same way the trends table does.
    """

    __tablename__ = "stock_analyst_rating_changes"
    __table_args__ = (
        UniqueConstraint(
            "stock_id",
            "firm",
            "published_at",
            name="uq_analyst_rating_changes_stock_firm_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    firm: Mapped[str] = mapped_column(String(length=128), nullable=False)
    published_at: Mapped[date] = mapped_column(Date, nullable=False)
    action: Mapped[str | None] = mapped_column(String(length=16), nullable=True)
    from_grade: Mapped[str | None] = mapped_column(String(length=64), nullable=True)
    to_grade: Mapped[str | None] = mapped_column(String(length=64), nullable=True)
    target_current: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_prior: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def trends_by_symbol(
    session: Session, symbol: str
) -> list[StockRecommendationTrendRecord]:
    """All stored trend rows for ``symbol`` (joined through the ``stocks`` anchor),
    ordered newest→oldest by period. Empty when nothing is stored for it yet."""
    return list(
        session.execute(
            select(StockRecommendationTrendRecord)
            .join(StockRecord, StockRecommendationTrendRecord.stock_id == StockRecord.id)
            .where(StockRecord.ticker == symbol)
            .order_by(StockRecommendationTrendRecord.period.desc())
        ).scalars()
    )


def delete_trends_for_periods(
    session: Session, stock_id: uuid.UUID, periods: Sequence[date]
) -> None:
    """Remove a stock's rows for exactly ``periods``, so a refresh can re-insert the
    months the source served while leaving earlier stored months intact (the merge)."""
    if not periods:
        return
    session.execute(
        delete(StockRecommendationTrendRecord).where(
            StockRecommendationTrendRecord.stock_id == stock_id,
            StockRecommendationTrendRecord.period.in_(periods),
        )
    )


def rating_changes_by_symbol(
    session: Session, symbol: str
) -> list[StockAnalystRatingChangeRecord]:
    """All stored rating-change rows for ``symbol`` (joined through the ``stocks`` anchor),
    newest action first. Empty when nothing is stored for it yet. The repository's
    insert-only upsert reads these to skip events it already holds."""
    return list(
        session.execute(
            select(StockAnalystRatingChangeRecord)
            .join(
                StockRecord,
                StockAnalystRatingChangeRecord.stock_id == StockRecord.id,
            )
            .where(StockRecord.ticker == symbol)
            .order_by(StockAnalystRatingChangeRecord.published_at.desc())
        ).scalars()
    )


def stalest_symbols(
    session: Session, limit: int | None = None
) -> list[tuple[str, str | None]]:
    """``(symbol, name)`` pairs from the ``stocks`` anchor, most in need of a refresh first.

    A **LEFT JOIN**, so every anchor stock is included — even one with no trend rows yet — and
    the sync both *seeds* new coverage and renews stale rows. Cached stocks are ordered by the
    *newest* fetch stamp among their rows (the merge keeps old stamps on old months forever, so
    the min would pin a long-cached stock permanently stale; the max is when it was last
    actually refreshed). Ordering is **un-cached first**: a never-fetched stock has a NULL max
    stamp and sorts ahead of any cached stock. ``limit`` caps the batch; ``None`` (the default)
    returns every stock, so one sweep can seed the whole anchor. Lazy fill on first access still
    covers a symbol between sweeps.
    """
    max_fetched = func.max(StockRecommendationTrendRecord.fetched_at)
    stmt = (
        select(StockRecord.ticker, StockRecord.name)
        .outerjoin(
            StockRecommendationTrendRecord,
            StockRecommendationTrendRecord.stock_id == StockRecord.id,
        )
        .group_by(StockRecord.id, StockRecord.ticker, StockRecord.name)
        # un-cached (NULL stamp) first, then least-recently-refreshed — portable NULLs-first.
        .order_by(max_fetched.is_(None).desc(), max_fetched.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(row.ticker, row.name) for row in session.execute(stmt).all()]
