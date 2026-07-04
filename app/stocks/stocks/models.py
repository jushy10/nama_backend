"""Database model + queries for the shared ``stocks`` anchor.

The ``stocks`` table is the single row every per-feature table (the earnings
timelines, …) points at, so the same stock is one thing everyone references rather than
a symbol string copied around. It's owned by no single feature, so it gets its own slice
here. Feature slices import ``StockRecord`` + ``get_or_create_stock`` and add their own
child tables beside it. The schema is created by migration 0002 (the since-removed
analyst-estimates feature was the first to need the anchor); migration 0009 added
``exchange`` and 0010 renamed the ``symbol`` column to ``ticker`` (the domain layers
still say "symbol" — the rename is a table-vocabulary choice).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Float, String, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column


from app.db import Base


class StockRecord(Base):
    """A stock as stored in the database — the anchor per-feature tables reference.

    ``id`` is a surrogate UUID so child rows have a stable foreign key; ``ticker`` is
    what everything is looked up by (unique); ``name`` is the company display name and
    ``exchange`` the listing venue (e.g. "NASDAQ") — both nullable so a lazily-stored
    ticker (which arrives alone) still gets a row until whichever feature first learns
    them fills them in.

    ``revenue_growth_yoy`` / ``eps_growth_yoy`` are the stock's *latest trailing*
    year-over-year growth (percent) — the newest reported fiscal year over the one
    before it, written by the annual-earnings slice from its stored timeline. Unlike
    ``name``/``exchange`` (fill-once identity facts) these are a moving snapshot:
    they're **overwritten** on every annual refresh as the latest reported year rolls
    forward, so a stock carries exactly one pair (the current one), not a history. The
    EPS figure is on the analyst-consensus (adjusted) basis, matching the annual
    slice's ``eps_actual_consensus``. Nullable — unset until the annual slice has two
    reported years cached (and EPS best-effort, since the consensus basis often isn't).
    """

    __tablename__ = "stocks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ticker: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revenue_growth_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_growth_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)


def get_or_create_stock(
    session: Session, ticker: str, name: str | None
) -> StockRecord:
    """Return the ``stocks`` row for ``ticker``, creating it if absent.

    Fills a missing name when one is supplied, but never clobbers a known name with
    ``None`` — so whichever feature first learns the company name sets it, and a later
    nameless write (e.g. an earnings refresh) leaves it intact. The new row is flushed
    so its ``id`` is available for a child row in the same unit of work.
    """
    stock = session.execute(
        select(StockRecord).where(StockRecord.ticker == ticker)
    ).scalar_one_or_none()
    if stock is None:
        stock = StockRecord(ticker=ticker, name=name)
        session.add(stock)
        session.flush()  # assign stock.id before a child row references it
    elif name and not stock.name:
        stock.name = name
    return stock


def anchor_facts(session: Session, ticker: str) -> tuple[str | None, str | None]:
    """The stored ``(name, exchange)`` for ``ticker`` in one query — ``(None, None)``
    when the row doesn't exist yet, and per-field ``None`` for whatever it hasn't
    learned. The misses a lazy fill answers."""
    row = session.execute(
        select(StockRecord.name, StockRecord.exchange).where(
            StockRecord.ticker == ticker
        )
    ).one_or_none()
    return (row.name, row.exchange) if row else (None, None)


def fill_exchange(session: Session, ticker: str, exchange: str) -> None:
    """Record ``ticker``'s listing exchange, creating the anchor row if absent.

    Same semantics as the name on ``get_or_create_stock``: fill when missing, never
    clobber a known value — an exchange effectively never changes, so the first
    feature to learn it settles it."""
    stock = get_or_create_stock(session, ticker, None)
    if not stock.exchange:
        stock.exchange = exchange
