"""Database model + queries for the shared ``stocks`` anchor.

The ``stocks`` table is the single row every per-feature table (analyst estimates, …)
points at, so the same stock is one thing everyone references rather than a symbol
string copied around. It's owned by no single feature, so it gets its own slice here.
Feature slices import ``StockRecord`` + ``get_or_create_stock`` and add their own child
tables beside it. The schema is created by the analyst-estimates migration (the first
feature to need the anchor).
"""

from __future__ import annotations

import uuid

from sqlalchemy import String, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base


class StockRecord(Base):
    """A stock as stored in the database — the anchor per-feature tables reference.

    ``id`` is a surrogate UUID so child rows have a stable foreign key; ``symbol`` is
    the ticker everything is looked up by (unique); ``name`` is the company display
    name, nullable so a lazily-stored symbol (which arrives with only its ticker)
    still gets a row until a sync fills the name in.
    """

    __tablename__ = "stocks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)


def get_or_create_stock(
    session: Session, symbol: str, name: str | None
) -> StockRecord:
    """Return the ``stocks`` row for ``symbol``, creating it if absent.

    Fills a missing name when one is supplied, but never clobbers a known name with
    ``None`` — so whichever feature first learns the company name sets it, and a later
    nameless write (e.g. an estimates refresh) leaves it intact. The new row is flushed
    so its ``id`` is available for a child row in the same unit of work.
    """
    stock = session.execute(
        select(StockRecord).where(StockRecord.symbol == symbol)
    ).scalar_one_or_none()
    if stock is None:
        stock = StockRecord(symbol=symbol, name=name)
        session.add(stock)
        session.flush()  # assign stock.id before a child row references it
    elif name and not stock.name:
        stock.name = name
    return stock
