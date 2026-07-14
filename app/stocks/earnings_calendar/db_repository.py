"""Interface Adapter: the SQLAlchemy-backed EarningsCalendarRepository.

Implements ``repository.py`` with one indexed join across ``stock_quarterly_earnings`` (the
scheduled dates) and the shared ``stocks`` anchor (name + sector). The slice owns no table —
it's a projection of rows the quarterly-earnings sync already wrote — so this is the only file
(alongside the models it reads) that knows those tables exist; the domain entities stay free
of SQLAlchemy.

The read is deliberately narrow: only *upcoming* quarters (``eps_actual IS NULL``) that carry
a scheduled ``report_date`` inside the window, ordered by date then ticker so the use case can
fold them into days without re-sorting.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.stocks.earnings_calendar.entities import EarningsCalendarItem
from app.stocks.earnings_calendar.repository import EarningsCalendarRepository
from app.stocks.earnings.quarterly.models import StockQuarterlyEarningsRecord
from app.stocks.stocks.models import StockRecord


class SqlEarningsCalendarRepository(EarningsCalendarRepository):
    """Reads the upcoming earnings calendar through a request-scoped session. Read-only."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upcoming(
        self, from_date: date, to_date: date, limit: int
    ) -> list[EarningsCalendarItem]:
        rows = self._session.execute(
            select(
                StockRecord.ticker,
                StockRecord.name,
                StockRecord.sector,
                StockQuarterlyEarningsRecord.report_date,
            )
            .join(
                StockRecord,
                StockQuarterlyEarningsRecord.stock_id == StockRecord.id,
            )
            .where(
                # An upcoming quarter is one that hasn't reported yet: eps_actual is the
                # single discriminator the quarterly slice documents (NULL until it reports).
                StockQuarterlyEarningsRecord.eps_actual.is_(None),
                StockQuarterlyEarningsRecord.report_date.is_not(None),
                StockQuarterlyEarningsRecord.report_date >= from_date,
                StockQuarterlyEarningsRecord.report_date <= to_date,
            )
            .order_by(
                StockQuarterlyEarningsRecord.report_date.asc(),
                StockRecord.ticker.asc(),
            )
            .limit(limit)
        ).all()
        return [
            EarningsCalendarItem(
                ticker=ticker,
                name=name,
                sector=sector,
                report_date=report_date,
            )
            for ticker, name, sector, report_date in rows
        ]
