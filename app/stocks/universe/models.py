"""Database model + queries for the stock-universe membership.

The persistence primitives for the slice: the SQLAlchemy model for the ``stock_universe``
table this feature owns, plus simple, entity-free query functions over it. The shared
``stocks`` anchor these rows hang off of lives in its own slice,
``app/stocks/stocks/models.py`` (owned by no single feature), and is imported here. The
concrete repository (``db_repository.py``) is the only caller; it maps these rows to and
from the ``ScreenedStock`` entity. Nothing here knows the domain entity — this layer deals
only in rows and columns.

A 1:1 child of ``stocks``: one row per stock recording that it's currently a member of the
screened ≥$5B universe, carrying the drifting ``market_cap`` / ``sector`` and the
``screened_at`` stamp of the last screen that included it. Unique on ``stock_id`` — unlike
the earnings / recommendations time series (many rows per stock), a stock is in the
universe once or not at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Iterable

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    delete,
    func,
    or_,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base

# The shared ``stocks`` anchor + its helpers, re-exported so the repository reaches them as
# ``models.StockRecord`` / ``models.get_or_create_stock``.
from app.stocks.stocks.models import (  # noqa: F401
    StockRecord,
    fill_exchange,
    get_or_create_stock,
)


class StockUniverseRecord(Base):
    """One stock's membership in the screened universe.

    A 1:1 child of the ``stocks`` anchor (unique ``stock_id``). ``market_cap`` is whole
    dollars and ``sector`` the screener's classification — both drift, so they live here
    rather than on the identity-only anchor. ``screened_at`` is when the last screen that
    included this stock ran; it doubles as the freshness stamp.
    """

    __tablename__ = "stock_universe"
    __table_args__ = (UniqueConstraint("stock_id", name="uq_stock_universe_stock"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stock_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    sector: Mapped[str | None] = mapped_column(String(64), nullable=True)
    screened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


def universe_by_ticker(session: Session) -> dict[str, StockUniverseRecord]:
    """Every stored universe row keyed by its stock's ticker (one JOIN query).

    The reconcile loads the whole set once up front to decide, per screened stock,
    insert-vs-update — and which stored tickers to remove — without a per-row lookup. The
    universe is small (~1,000–1,300 rows), so a single SELECT beats N round-trips.
    """
    result: dict[str, StockUniverseRecord] = {}
    for record, ticker in session.execute(
        select(StockUniverseRecord, StockRecord.ticker).join(
            StockRecord, StockUniverseRecord.stock_id == StockRecord.id
        )
    ).all():
        result[ticker] = record
    return result


def delete_universe_absent(session: Session, keep_tickers: Iterable[str]) -> int:
    """Delete universe rows whose stock's ticker is not in ``keep_tickers``; return how
    many were removed. The anchor ``stocks`` rows are left intact (other slices reference
    them). ``keep_tickers`` must be non-empty — the caller only reconciles against a
    complete screen — so an empty set is a no-op guard rather than "delete everything"."""
    keep = set(keep_tickers)
    if not keep:
        return 0
    ids = (
        session.execute(
            select(StockUniverseRecord.id)
            .join(StockRecord, StockUniverseRecord.stock_id == StockRecord.id)
            .where(StockRecord.ticker.notin_(keep))
        )
        .scalars()
        .all()
    )
    if not ids:
        return 0
    session.execute(
        delete(StockUniverseRecord).where(StockUniverseRecord.id.in_(ids))
    )
    return len(ids)


def search_universe(session: Session, query: str, limit: int) -> list:
    """Up to ``limit`` universe members whose ticker or name matches ``query`` (a
    case-insensitive substring), largest market cap first. Returns column Rows
    (``ticker`` / ``name`` / ``exchange`` / ``market_cap`` / ``sector``) for the repository
    to map. ``coalesce(market_cap, 0)`` keeps a rare null-cap member sorting last portably
    (Postgres would otherwise sort NULLs first under ``DESC``)."""
    like = f"%{query}%"
    return list(
        session.execute(
            select(
                StockRecord.ticker,
                StockRecord.name,
                StockRecord.exchange,
                StockUniverseRecord.market_cap,
                StockUniverseRecord.sector,
            )
            .join(
                StockUniverseRecord,
                StockUniverseRecord.stock_id == StockRecord.id,
            )
            .where(
                or_(StockRecord.ticker.ilike(like), StockRecord.name.ilike(like))
            )
            .order_by(func.coalesce(StockUniverseRecord.market_cap, 0.0).desc())
            .limit(limit)
        ).all()
    )
