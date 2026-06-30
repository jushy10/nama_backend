"""The shared ``stocks`` anchor model and a get-or-create helper.

Per-feature tables (analyst estimates, company profile, …) hang off a single
``stocks`` row rather than copying the symbol string around, so the same stock is
one thing every child row points at. The anchor lives here on its own — owned by no
single feature — and each feature module imports ``StockRecord`` and adds its own
child table beside it. The schema for ``stocks`` itself is created by the analyst-
estimates migration (the first feature to need the anchor).
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
    name, nullable so a lazily-stored symbol (which may arrive with only its ticker)
    still gets a row until something fills the name in.
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
    ``None`` — so whichever feature first learns the company name sets it, and a
    later nameless write (e.g. an estimates refresh) leaves it intact. The new row is
    flushed so its ``id`` is available for a child row in the same unit of work.
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
