"""Interface Adapter: the SQLAlchemy-backed FundamentalsRepository.

Implements ``repository.py`` against the shared ``stocks`` anchor — fundamentals have no table
of their own, so the figures are written straight onto ``stocks`` (the profitability / health
columns plus the per-share inputs and the ``fundamentals_synced_at`` stamp). Only this layer
touches SQLAlchemy. ``upsert`` commits its own write, so a successful refresh is durable
independent of the surrounding request; ``refresh_targets`` is the stale-first read the sweep
drives off.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.stocks.fundamentals.entities import Fundamentals
from app.stocks.fundamentals.repository import FundamentalsRepository, RefreshTarget
from app.stocks.stocks.models import StockRecord, get_or_create_stock


class SqlFundamentalsRepository(FundamentalsRepository):
    """Reads the stale-first work-list and writes fundamentals onto the ``stocks`` anchor
    through a request-scoped session. ``upsert`` commits its own write so a successful refresh
    is durable independent of the surrounding request."""

    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the sync stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        # Every anchor stock, un-synced first then stalest — the freshness stamp is a single
        # column on `stocks`, so (unlike the earnings slices' min-over-child-rows) this is a
        # plain order over the anchor with a portable NULLS-first ordering. `None` limit returns
        # the whole anchor so one sweep can seed it all.
        synced = StockRecord.fundamentals_synced_at
        stmt = (
            select(StockRecord.ticker, StockRecord.name)
            .order_by(synced.is_(None).desc(), synced.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return [
            RefreshTarget(row.ticker, row.name)
            for row in self._session.execute(stmt).all()
        ]

    def upsert(
        self, symbol: str, name: str | None, fundamentals: Fundamentals
    ) -> None:
        stock = get_or_create_stock(self._session, symbol, name)
        # Overwrite every column (including to None) — a moving snapshot, like the growth/cash
        # pair the annual slice writes: a figure Yahoo has since dropped is cleared, not left
        # stale. The entity owns the figures; this layer just lands them on the row.
        stock.gross_margin = fundamentals.gross_margin
        stock.operating_margin = fundamentals.operating_margin
        stock.net_margin = fundamentals.net_margin
        stock.return_on_equity = fundamentals.return_on_equity
        stock.current_ratio = fundamentals.current_ratio
        stock.debt_to_equity = fundamentals.debt_to_equity
        stock.beta = fundamentals.beta
        stock.book_value_per_share = fundamentals.book_value_per_share
        stock.sales_per_share = fundamentals.sales_per_share
        stock.dividend_per_share = fundamentals.dividend_per_share
        stock.fundamentals_synced_at = self._now()
        self._session.commit()
