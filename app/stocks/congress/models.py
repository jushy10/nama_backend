"""Database model + queries for the Congressional-trades cache.

The persistence primitives for the slice: the SQLAlchemy model for the ``stock_congress_trades``
table this feature owns, plus simple, entity-free query functions over it. The shared ``stocks``
anchor these rows hang off of lives in its own slice, ``app/stocks/stocks/models.py`` (owned by no
single feature), and is imported here. The concrete repository (``db_repository.py``) is the only
caller; it maps these rows to and from the ``CongressTrade`` entity. Nothing here knows the domain
entity — this layer deals only in rows and columns, so it stays a thin data-access layer.

A time series: many rows per stock, one per disclosed trade, keyed unique on
``(stock_id, member, transaction_date, amount_range, chamber)`` — the contract's identity for a
Congressional disclosure. Like the insider / rating-changes slices a refresh is *insert-only* (a
filed disclosure is a frozen fact), and like the news feed the accumulated history is **pruned** to
the newest ``keep`` trades per stock so it stays bounded. ``fetched_at`` is a cache-bookkeeping
stamp — the as-of time of the last fetch that covered the stock, refreshed on every upsert so the
out-of-band sweep (``stalest_symbols``) can order stocks by how recently they were confirmed.

Ordering everywhere is by **activity date** — the disclosure date (when the trade became public)
falling back to the transaction date — so a board reads "most recently disclosed first", the
standard Congress-tracking convention (a member has 45 days to disclose, so transaction dates lag).
"""

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
from app.stocks.stocks.models import StockRecord, get_or_create_stock  # noqa: F401


class StockCongressTradeRecord(Base):
    """One member's one disclosed trade in a stock.

    ``member`` + ``transaction_date`` + ``amount_range`` + ``chamber`` form the row's unique key
    alongside ``stock_id`` (the contract's identity for a Congressional disclosure). ``tx_type`` is
    the normalized action (``Purchase`` / ``Sale`` / ``Exchange`` / ``Other``). Free-text fields are
    sized generously and the adapter clips to the same widths so a pathological outlier can never
    overflow a column on Postgres and (silently, via the swallowed cache write) poison the stock's
    cache. ``party`` is nullable — the keyless feeds don't carry it.
    """

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
    """The single date column trades are ordered/windowed by: disclosure date (when the trade
    became public) falling back to the transaction date."""
    return func.coalesce(
        StockCongressTradeRecord.disclosure_date,
        StockCongressTradeRecord.transaction_date,
    )


def _order_newest_first() -> tuple:
    """The canonical serving/pruning order: newest *activity date* first, then a stable tiebreak so
    the order is deterministic across rows sharing a date (and identical to the adapter's own sort,
    so a live-served and cache-served response match)."""
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
    """All stored trade rows for ``symbol`` (joined through the ``stocks`` anchor), newest first.
    Empty when nothing is stored for it yet."""
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
    """A page of the whole market's recent trades as ``(record, ticker, name)`` rows, newest
    first. ``since`` (inclusive) windows on the activity date; ``None`` means no window (all
    history). ``limit`` / ``offset`` cut the page."""
    stmt = (
        select(StockCongressTradeRecord, StockRecord.ticker, StockRecord.name)
        .join(StockRecord, StockCongressTradeRecord.stock_id == StockRecord.id)
    )
    if since is not None:
        stmt = stmt.where(_activity_date() >= since)
    stmt = stmt.order_by(*_order_newest_first()).limit(limit).offset(offset)
    return session.execute(stmt).all()


def count_recent_market_trades(session: Session, *, since: date | None) -> int:
    """The full count of market-wide trades in the window (before the page is cut) — what the
    endpoint reports as ``total`` so a client can size its pager."""
    stmt = select(func.count()).select_from(StockCongressTradeRecord)
    if since is not None:
        stmt = stmt.where(_activity_date() >= since)
    return int(session.execute(stmt).scalar_one())


def stalest_symbols(
    session: Session, limit: int | None = None
) -> list[tuple[str, str | None]]:
    """``(symbol, name)`` pairs from the ``stocks`` anchor, most in need of a refresh first.

    A **LEFT JOIN**, so every anchor stock is included — even one with no trade rows yet — and the
    sweep both *seeds* new coverage and renews stale rows. Cached stocks are ordered by the *newest*
    fetch stamp among their rows (``touch_fetched_at`` moves every row's stamp to the same as-of
    time on each upsert, so the max is when the stock was last confirmed). Ordering is **un-cached
    first**: a never-fetched stock has a NULL max stamp and sorts ahead of any cached stock.
    ``limit`` caps the batch; ``None`` (the default) returns every stock, so one sweep can seed the
    whole anchor.
    """
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
    """The ``(member, transaction_date, amount_range, chamber)`` keys already stored for
    ``stock_id`` — what the insert-only upsert diffs the fresh trades against."""
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
    """Refresh every stored row's ``fetched_at`` for ``stock_id`` to ``now`` — the as-of time of
    this fetch. Called on every upsert (even one that inserts no new rows) so a stock the source
    confirmed with no new activity still reads as fresh to the sweep's staleness order."""
    session.execute(
        update(StockCongressTradeRecord)
        .where(StockCongressTradeRecord.stock_id == stock_id)
        .values(fetched_at=now)
    )


def prune_to_newest(session: Session, stock_id: uuid.UUID, keep: int) -> None:
    """Delete all but the ``keep`` newest trades for ``stock_id`` so the accumulated feed stays
    bounded. Selects the row ids in serving order and deletes the surplus tail — portable across
    SQLite/Postgres."""
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
