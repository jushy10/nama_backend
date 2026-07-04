"""Database queries for the universe slice, over the shared ``stocks`` anchor.

The universe has **no table of its own**: the screen is folded straight into ``stocks``
(the ``sector`` / ``market_cap`` / ``screened_at`` columns migration 0011 added). This
module holds the slice's entity-free queries over that anchor — currently just search — and
re-exports the anchor model + ``get_or_create_stock`` the concrete repository reaches as
``models.StockRecord`` / ``models.get_or_create_stock``. The shared ``stocks`` slice
(``app/stocks/stocks/models.py``) owns them; nothing here knows the ``ScreenedStock`` entity.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

# The shared ``stocks`` anchor + its create helper, re-exported so the repository reaches
# them as ``models.StockRecord`` / ``models.get_or_create_stock``.
from app.stocks.stocks.models import (  # noqa: F401
    StockRecord,
    get_or_create_stock,
)


def search_screened(session: Session, query: str, limit: int) -> list:
    """Up to ``limit`` screened stocks whose ticker or name matches ``query`` (a
    case-insensitive substring), largest market cap first. Returns column Rows
    (``ticker`` / ``name`` / ``exchange`` / ``market_cap`` / ``sector``) for the repository
    to map.

    Screened members only: the ``market_cap IS NOT NULL`` filter excludes anchors that
    reached ``stocks`` some other way (a ticker-card lookup, an earnings refresh) and were
    never part of the screen — they carry no market cap. Because every returned row has a
    market cap, the ``DESC`` sort needs no null handling.
    """
    like = f"%{query}%"
    return list(
        session.execute(
            select(
                StockRecord.ticker,
                StockRecord.name,
                StockRecord.exchange,
                StockRecord.market_cap,
                StockRecord.sector,
            )
            .where(StockRecord.market_cap.is_not(None))
            .where(or_(StockRecord.ticker.ilike(like), StockRecord.name.ilike(like)))
            .order_by(StockRecord.market_cap.desc())
            .limit(limit)
        ).all()
    )
