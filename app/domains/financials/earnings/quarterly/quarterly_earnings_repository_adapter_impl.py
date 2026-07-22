from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.domains.financials.earnings.quarterly import models
from app.domains.financials.earnings.quarterly.entities import (
    EarningsSession,
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.domains.financials.earnings.quarterly.models import StockQuarterlyEarningsRecord
from app.domains.financials.earnings.quarterly.interfaces import (
    QuarterlyEarningsRepositoryAdapter,
    RefreshTarget,
)


def _quarter_key(quarter: QuarterlyEarnings) -> tuple[int, int]:
    return (quarter.fiscal_year, quarter.fiscal_quarter)


def _session_from_str(value: str | None) -> EarningsSession:
    if value is None:
        return EarningsSession.UNKNOWN
    try:
        return EarningsSession(value)
    except ValueError:
        return EarningsSession.UNKNOWN


def _to_entity(row: StockQuarterlyEarningsRecord) -> QuarterlyEarnings:
    return QuarterlyEarnings(
        fiscal_year=row.fiscal_year,
        fiscal_quarter=row.fiscal_quarter,
        period_end=row.period_end,
        report_date=row.report_date,
        eps_actual=row.eps_actual,
        eps_estimate=row.eps_estimate,
        eps_surprise=row.eps_surprise,
        eps_surprise_percent=row.eps_surprise_percent,
        revenue_estimate=row.revenue_estimate,
        revenue_actual=row.revenue_actual,
        report_session=_session_from_str(row.report_session),
    )


def _to_timeline(
    symbol: str, rows: list[StockQuarterlyEarningsRecord]
) -> QuarterlyEarningsTimeline:
    quarters = sorted((_to_entity(row) for row in rows), key=_quarter_key)
    return QuarterlyEarningsTimeline(symbol=symbol, quarters=tuple(quarters))


class QuarterlyEarningsRepositoryAdapterImpl(QuarterlyEarningsRepositoryAdapter):
    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the fetch stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get(self, symbol: str) -> QuarterlyEarningsTimeline | None:
        rows = models.quarters_by_symbol(self._session, symbol)
        if not rows:
            return None
        return _to_timeline(symbol, rows)

    def upsert(
        self, symbol: str, name: str | None, timeline: QuarterlyEarningsTimeline
    ) -> None:
        stock = models.get_or_create_stock(self._session, symbol, name)

        # Rewrite the whole window: clear the stock's rows, then insert the new set.
        # Simpler and correct for a variable-length set of quarters than diffing.
        models.delete_quarters_for_stock(self._session, stock.id)
        now = self._now()
        for quarter in timeline.quarters:
            self._session.add(
                StockQuarterlyEarningsRecord(
                    stock_id=stock.id,
                    fiscal_year=quarter.fiscal_year,
                    fiscal_quarter=quarter.fiscal_quarter,
                    period_end=quarter.period_end,
                    report_date=quarter.report_date,
                    eps_actual=quarter.eps_actual,
                    eps_estimate=quarter.eps_estimate,
                    eps_surprise=quarter.eps_surprise,
                    eps_surprise_percent=quarter.eps_surprise_percent,
                    revenue_estimate=quarter.revenue_estimate,
                    revenue_actual=quarter.revenue_actual,
                    report_session=quarter.report_session.value,
                    fetched_at=now,
                )
            )
        self._session.commit()

    def refresh_targets(self, limit: int | None) -> list[RefreshTarget]:
        # Delegates the query to models (un-cached first, then stalest); this layer just
        # wraps each (symbol, name) pair in the domain-facing RefreshTarget.
        return [
            RefreshTarget(symbol, name)
            for symbol, name in models.stalest_symbols(self._session, limit)
        ]
