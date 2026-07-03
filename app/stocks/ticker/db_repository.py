"""Interface Adapter: the SQLAlchemy-backed TickerRepository.

Implements the ``repository.py`` port against the database. The slice owns no table —
the facts it persists (the display name and the listing exchange) live on the shared
``stocks`` anchor, so this delegates entirely to the anchor slice's query helpers
(``app/stocks/stocks/models.py``; the name fill *is* ``get_or_create_stock``'s
fill-but-never-clobber). The saves commit their own write so a successful lazy fill
is durable independent of the surrounding request.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.stocks.stocks import models
from app.stocks.ticker.repository import StoredTickerFacts, TickerRepository


class SqlTickerRepository(TickerRepository):
    """Reads and writes the anchor-level ticker facts through a request-scoped session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_facts(self, symbol: str) -> StoredTickerFacts:
        return StoredTickerFacts(*models.anchor_facts(self._session, symbol))

    def save_name(self, symbol: str, name: str) -> None:
        models.get_or_create_stock(self._session, symbol, name)
        self._session.commit()

    def save_exchange(self, symbol: str, exchange: str) -> None:
        models.fill_exchange(self._session, symbol, exchange)
        self._session.commit()
