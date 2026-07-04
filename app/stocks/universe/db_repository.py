"""Interface Adapter: the SQLAlchemy-backed UniverseRepository.

Implements the ``repository.py`` port against the database. Its job is the mapping the use
cases must not see: it converts ``ScreenedStock`` entities to and from the ORM rows and
delegates every query to ``models.py``. Only this layer (and models) knows the tables
exist; the domain entities stay free of SQLAlchemy. ``replace_universe`` commits its own
write, so a successful sync is durable independent of the request.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.stocks.stocks.models import get_or_create_stock
from app.stocks.universe import models
from app.stocks.universe.entities import ScreenedStock
from app.stocks.universe.models import StockUniverseRecord
from app.stocks.universe.repository import UniverseRepository, UniverseSyncCounts


class SqlUniverseRepository(UniverseRepository):
    """Reads and writes the universe through a request-scoped session.

    Holds the session the endpoint injects via ``get_db``, maps rows to and from the
    ``ScreenedStock`` entity, and delegates every query to ``models``. ``replace_universe``
    commits its own write so a successful sync is durable independent of the surrounding
    request.
    """

    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the screen stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def replace_universe(
        self, stocks: tuple[ScreenedStock, ...]
    ) -> UniverseSyncCounts:
        # Load the whole stored universe once (keyed by ticker) so each screened stock is
        # an in-memory insert-vs-update decision, not a per-row SELECT.
        existing = models.universe_by_ticker(self._session)
        now = self._now()
        added = 0
        updated = 0
        seen: set[str] = set()

        for stock in stocks:
            seen.add(stock.ticker)
            # Fill the anchor: create the row if new, set the display name when supplied
            # (never clobbering a known one — get_or_create_stock enforces that).
            anchor = get_or_create_stock(self._session, stock.ticker, stock.name)
            # Same fill-once rule for the exchange: settle it the first time we learn it.
            if stock.exchange and not anchor.exchange:
                anchor.exchange = stock.exchange

            row = existing.get(stock.ticker)
            if row is None:
                self._session.add(
                    StockUniverseRecord(
                        stock_id=anchor.id,
                        market_cap=stock.market_cap,
                        sector=stock.sector,
                        screened_at=now,
                    )
                )
                added += 1
            else:
                # Refresh the drifting figures + freshness stamp in place.
                row.market_cap = stock.market_cap
                row.sector = stock.sector
                row.screened_at = now
                updated += 1

        # Reconcile: drop members the screen no longer lists (fell below the floor /
        # delisted). Their anchor rows survive — only the membership row goes.
        removed = models.delete_universe_absent(self._session, seen)
        self._session.commit()
        return UniverseSyncCounts(added=added, updated=updated, removed=removed)

    def search(self, query: str, *, limit: int) -> tuple[ScreenedStock, ...]:
        return tuple(
            ScreenedStock(
                ticker=row.ticker,
                name=row.name,
                exchange=row.exchange,
                market_cap=row.market_cap,
                sector=row.sector,
            )
            for row in models.search_universe(self._session, query, limit)
        )
