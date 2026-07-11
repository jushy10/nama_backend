"""Interface Adapter: the SQLAlchemy-backed SeoReadRepository.

Implements ``repository.py`` against the shared ``stocks`` anchor. The slice owns no
table — a content page is a projection of columns other slices' syncs already wrote
onto the anchor — so this is a single indexed read, no joins, no vendor, no key. That's
the whole point: a crawler hitting the page pays one DB round-trip, never a live fetch.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.stocks.seo.repository import (
    SectorStock,
    SeoReadRepository,
    StockPageRef,
    TickerPageFacts,
)
from app.stocks.stocks.models import StockRecord


class SqlSeoReadRepository(SeoReadRepository):
    """Reads the content-page facts off the ``stocks`` anchor through a request-scoped
    session. Read-only; a page never writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_ticker_facts(self, ticker: str) -> TickerPageFacts | None:
        row = self._session.execute(
            select(
                StockRecord.name,
                StockRecord.exchange,
                StockRecord.sector,
                StockRecord.industry,
                StockRecord.market_cap,
                StockRecord.pe_ratio,
                StockRecord.fcf_yield,
                StockRecord.revenue_growth_yoy,
                StockRecord.eps_growth_yoy,
                StockRecord.fcf_growth_yoy,
                StockRecord.in_sp500,
                StockRecord.in_nasdaq100,
            ).where(StockRecord.ticker == ticker)
        ).one_or_none()
        if row is None:
            return None
        # The SELECT column order matches TickerPageFacts' field order, so the row
        # unpacks straight onto it.
        return TickerPageFacts(*row)

    def list_stock_pages(self, limit: int) -> tuple[StockPageRef, ...]:
        rows = self._session.execute(
            select(StockRecord.ticker, StockRecord.screened_at)
            .where(StockRecord.market_cap.is_not(None))  # screened / index-worthy only
            .order_by(StockRecord.market_cap.desc(), StockRecord.ticker)
            .limit(limit)
        ).all()
        return tuple(
            StockPageRef(
                ticker=ticker,
                # screened_at is a tz-aware datetime; the sitemap wants a date.
                last_modified=screened_at.date() if screened_at is not None else None,
            )
            for ticker, screened_at in rows
        )

    def list_sector_stocks(self, sector: str, limit: int) -> tuple[SectorStock, ...]:
        rows = self._session.execute(
            select(
                StockRecord.ticker,
                StockRecord.name,
                StockRecord.market_cap,
                StockRecord.pe_ratio,
                StockRecord.fcf_yield,
            )
            .where(
                StockRecord.market_cap.is_not(None),  # screened only
                StockRecord.sector == sector,
            )
            .order_by(StockRecord.market_cap.desc(), StockRecord.ticker)
            .limit(limit)
        ).all()
        # SELECT order matches SectorStock's fields, so each row unpacks straight onto it.
        return tuple(SectorStock(*row) for row in rows)

    def list_sectors(self) -> tuple[str, ...]:
        rows = (
            self._session.execute(
                select(StockRecord.sector)
                .where(
                    StockRecord.market_cap.is_not(None),
                    StockRecord.sector.is_not(None),
                )
                .distinct()
                .order_by(StockRecord.sector)
            )
            .scalars()
            .all()
        )
        return tuple(rows)
