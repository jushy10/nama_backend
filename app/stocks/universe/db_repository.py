"""Interface Adapter: the SQLAlchemy-backed UniverseRepository.

Implements ``repository.py`` against the shared ``stocks`` anchor — the universe has no
table of its own, so the screen is written straight onto ``stocks`` (ticker/name/exchange
plus the denormalized ``sector``/``market_cap``/``screened_at`` columns). Maps
``ScreenedStock`` entities to and from anchor rows and delegates queries to ``models.py``;
only this layer (and models) touches SQLAlchemy. ``upsert_screen`` commits its own write,
so a successful sync is durable independent of the request.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.stocks.universe import models
from app.stocks.universe.entities import ScreenedStock
from app.stocks.universe.repository import UniverseRepository, UniverseSyncCounts


class SqlUniverseRepository(UniverseRepository):
    """Reads and writes the universe through a request-scoped session, on the ``stocks``
    anchor. Maps rows to and from ``ScreenedStock``; ``upsert_screen`` commits its own write
    so a successful sync is durable independent of the surrounding request.
    """

    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the screen stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def upsert_screen(
        self, stocks: tuple[ScreenedStock, ...]
    ) -> UniverseSyncCounts:
        now = self._now()
        added = 0
        updated = 0
        for stock in stocks:
            anchor = models.get_or_create_stock(
                self._session, stock.ticker, stock.name
            )
            # A stock is "added" the first time the screen marks it (screened_at still
            # null) — whether the anchor is brand new or predates the screen; else it's an
            # in-place refresh.
            if anchor.screened_at is None:
                added += 1
            else:
                updated += 1
            # Fill identity facts when missing; never clobber a settled value (the same
            # rule get_or_create_stock applies to the name).
            if stock.exchange and not anchor.exchange:
                anchor.exchange = stock.exchange
            if stock.sector and not anchor.sector:
                anchor.sector = stock.sector
            # Refresh the drifting screen facts + freshness stamp on every run.
            anchor.market_cap = stock.market_cap
            anchor.screened_at = now
        self._session.commit()
        return UniverseSyncCounts(added=added, updated=updated)

    def search(self, query: str, *, limit: int) -> tuple[ScreenedStock, ...]:
        return tuple(
            ScreenedStock(
                ticker=row.ticker,
                name=row.name,
                exchange=row.exchange,
                market_cap=row.market_cap,
                sector=row.sector,
            )
            for row in models.search_screened(self._session, query, limit)
        )
