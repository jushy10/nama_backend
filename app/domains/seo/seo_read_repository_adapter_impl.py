from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domains.research.brief.models import recent_brief_dates
from app.domains.ownership.congress.models import StockCongressTradeRecord, _order_newest_first
from app.domains.etfs.models import EtfRecord
from app.domains.seo.interfaces import (
    CongressPageTrade,
    EtfPageFacts,
    SectorStock,
    SeoReadRepositoryAdapter,
    StockPageRef,
    TickerPageFacts,
)
from app.domains.listings.anchor.models import StockRecord

# The "best-of" screens sort by one of these anchor columns; the use-case registry names the
# key, the adapter owns the column mapping (so the use case never imports the model).
_SCREEN_SORT_COLUMNS = {
    "fcf_yield": StockRecord.fcf_yield,
    "pe_ratio": StockRecord.pe_ratio,
    "revenue_growth_yoy": StockRecord.revenue_growth_yoy,
    "market_cap": StockRecord.market_cap,
}


class SeoReadRepositoryAdapterImpl(SeoReadRepositoryAdapter):
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

    def list_screen_stocks(
        self, sort_key: str, *, descending: bool, positive_only: bool, limit: int
    ) -> tuple[SectorStock, ...]:
        column = _SCREEN_SORT_COLUMNS[sort_key]
        conditions = [
            StockRecord.market_cap.is_not(None),  # screened only
            column.is_not(None),  # must carry the ranked figure
        ]
        if positive_only:
            conditions.append(column > 0)
        ordering = column.desc() if descending else column.asc()
        rows = self._session.execute(
            select(
                StockRecord.ticker,
                StockRecord.name,
                StockRecord.market_cap,
                StockRecord.pe_ratio,
                StockRecord.fcf_yield,
            )
            .where(*conditions)
            .order_by(ordering, StockRecord.ticker)
            .limit(limit)
        ).all()
        return tuple(SectorStock(*row) for row in rows)

    def get_etf_facts(self, ticker: str) -> EtfPageFacts | None:
        row = self._session.execute(
            select(
                EtfRecord.name,
                EtfRecord.exchange,
                EtfRecord.category,
                EtfRecord.net_assets,
                EtfRecord.expense_ratio,
                EtfRecord.fund_family,
                EtfRecord.dividend_yield,
                EtfRecord.nav,
                EtfRecord.description,
            ).where(EtfRecord.ticker == ticker)
        ).one_or_none()
        if row is None:
            return None
        # SELECT order matches EtfPageFacts' fields, so the row unpacks straight onto it.
        return EtfPageFacts(*row)

    def list_etf_pages(self, limit: int) -> tuple[StockPageRef, ...]:
        rows = self._session.execute(
            select(EtfRecord.ticker, EtfRecord.screened_at)
            .where(EtfRecord.net_assets.is_not(None))  # screened / index-worthy
            .order_by(EtfRecord.net_assets.desc(), EtfRecord.ticker)
            .limit(limit)
        ).all()
        return tuple(
            StockPageRef(
                ticker=ticker,
                last_modified=screened_at.date() if screened_at is not None else None,
            )
            for ticker, screened_at in rows
        )

    def list_brief_dates(self, limit: int) -> tuple[date, ...]:
        # Reads the brief store directly (the slice owns no anchor column for this) —
        # newest-first, capped, so the sitemap lists the recent dated brief pages.
        return tuple(recent_brief_dates(self._session, limit))

    def list_recent_congress_trades(self, limit: int) -> tuple[CongressPageTrade, ...]:
        rows = self._session.execute(
            select(StockCongressTradeRecord, StockRecord.ticker, StockRecord.name)
            .join(StockRecord, StockCongressTradeRecord.stock_id == StockRecord.id)
            .order_by(*_order_newest_first())
            .limit(limit)
        ).all()
        return tuple(
            _to_congress_page_trade(row[0], ticker=row.ticker, name=row.name)
            for row in rows
        )

    def list_congress_trades_for_ticker(
        self, ticker: str, limit: int
    ) -> tuple[CongressPageTrade, ...]:
        rows = self._session.execute(
            select(StockCongressTradeRecord, StockRecord.name)
            .join(StockRecord, StockCongressTradeRecord.stock_id == StockRecord.id)
            .where(StockRecord.ticker == ticker)
            .order_by(*_order_newest_first())
            .limit(limit)
        ).all()
        return tuple(
            _to_congress_page_trade(row[0], ticker=ticker, name=row.name) for row in rows
        )


def _to_congress_page_trade(
    row: StockCongressTradeRecord, *, ticker: str, name: str | None
) -> CongressPageTrade:
    return CongressPageTrade(
        ticker=ticker,
        name=name,
        member=row.member,
        chamber=row.chamber,
        tx_type=row.tx_type,
        amount_range=row.amount_range,
        transaction_date=row.transaction_date,
        disclosure_date=row.disclosure_date,
    )
