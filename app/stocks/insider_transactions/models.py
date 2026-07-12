"""Database model + queries for the insider-transactions cache.

The persistence primitives for the slice: the SQLAlchemy model for the
``stock_insider_transactions`` table this feature owns, plus simple, entity-free query functions
over it. The shared ``stocks`` anchor these rows hang off of lives in its own slice,
``app/stocks/stocks/models.py`` (owned by no single feature), and is imported here. The concrete
repository (``db_repository.py``) is the only caller; it maps these rows to and from the
``InsiderTransaction`` entity. Nothing here knows the domain entity — this layer deals only in
rows and columns, so it stays a thin data-access layer.

A time series: many rows per stock, one per reported transaction, keyed unique on
``(stock_id, accession_number, line_index)`` — the filing's accession number plus the
transaction's ordinal within that filing. Like the rating-changes slice a refresh is
*insert-only* (a filed transaction is a frozen fact), and like the news feed the accumulated
history is **pruned** to the newest ``keep`` transactions per stock so it stays bounded.
``fetched_at`` is a cache-bookkeeping stamp — the as-of time of the last fetch that covered the
stock, refreshed on every upsert so the TTL read-through cache knows whether to re-fetch.
"""

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
from app.stocks.stocks.models import StockRecord, get_or_create_stock  # noqa: F401


class StockInsiderTransactionRecord(Base):
    """One insider's one reported transaction in a stock (a Form 4 non-derivative line).

    ``accession_number`` (the SEC filing id) + ``line_index`` (the transaction's ordinal within
    that filing) form the row's unique key alongside ``stock_id``. ``transaction_code`` is the
    raw Form 4 code (``P``/``S``/``M``/``F``/…) and ``acquired_disposed`` is ``A`` or ``D``.
    ``shares`` / ``price_per_share`` are nullable — a Form 4 sometimes reports a price only in a
    footnote — so the derived dollar value is best-effort.
    """

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
    """The canonical serving/pruning order: newest transaction first, falling back to the filing
    date when a transaction date is missing, then a stable tiebreak so the order is deterministic
    across rows sharing a date. Kept **identical** to the SEC adapter's own sort so a live-served
    and a cache-served response are the same regardless of cache state — the last leg keeps a
    filing's transactions in document order (``line_index`` ascending)."""
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
    """All stored transaction rows for ``symbol`` (joined through the ``stocks`` anchor), newest
    first. Empty when nothing is stored for it yet."""
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


def latest_fetched_at(session: Session, symbol: str) -> datetime | None:
    """The newest ``fetched_at`` among ``symbol``'s stored rows, or ``None`` when nothing is
    stored — the freshness the TTL cache checks."""
    return session.execute(
        select(func.max(StockInsiderTransactionRecord.fetched_at))
        .join(StockRecord, StockInsiderTransactionRecord.stock_id == StockRecord.id)
        .where(StockRecord.ticker == symbol)
    ).scalar()


def existing_keys_for_stock(
    session: Session, stock_id: uuid.UUID
) -> set[tuple[str, int]]:
    """The ``(accession_number, line_index)`` keys already stored for ``stock_id`` — what the
    insert-only upsert diffs the fresh transactions against."""
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
    """Refresh every stored row's ``fetched_at`` for ``stock_id`` to ``now`` — the as-of time of
    this fetch. Called on every upsert (even one that inserts no new rows) so a quiet stock the
    source confirmed with no new activity still reads as fresh, and a repeat view within the TTL
    is served from the DB rather than re-fetched from EDGAR."""
    session.execute(
        update(StockInsiderTransactionRecord)
        .where(StockInsiderTransactionRecord.stock_id == stock_id)
        .values(fetched_at=now)
    )


def prune_to_newest(session: Session, stock_id: uuid.UUID, keep: int) -> None:
    """Delete all but the ``keep`` newest transactions for ``stock_id`` so the accumulated feed
    stays bounded. Selects the row ids in serving order and deletes the surplus tail — portable
    across SQLite/Postgres."""
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
