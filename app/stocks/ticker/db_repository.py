"""Interface Adapter: the SQLAlchemy-backed TickerRepository.

Implements the ``repository.py`` port against the database. The slice owns no table —
the one fact it persists (the listing exchange) lives on the shared ``stocks`` anchor,
so this delegates entirely to the anchor slice's query helpers
(``app/stocks/stocks/models.py``). ``save_exchange`` commits its own write so a
successful lazy fill is durable independent of the surrounding request.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.stocks.stocks import models
from app.stocks.ticker.repository import TickerRepository


class SqlTickerRepository(TickerRepository):
    """Reads and writes the anchor-level ticker facts through a request-scoped session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_exchange(self, symbol: str) -> str | None:
        return models.exchange_by_symbol(self._session, symbol)

    def save_exchange(self, symbol: str, exchange: str) -> None:
        models.fill_exchange(self._session, symbol, exchange)
        self._session.commit()
