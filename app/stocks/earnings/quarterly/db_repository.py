"""Interface Adapter: the SQLAlchemy-backed QuarterlyEarningsRepository.

Implements the ``repository.py`` port against the database. Its job is the mapping the
use cases must not see: it converts the ``QuarterlyEarnings`` entities to and from the
ORM rows, and delegates every query to ``models.py``. Only this layer (and models) knows
the tables exist; the domain entities stay free of SQLAlchemy. ``upsert`` rewrites a
stock's whole window (delete-then-insert) and commits its own write, so a successful
cache fill is durable independent of the request.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.stocks.earnings.quarterly import models
from app.stocks.earnings.quarterly.entities import (
    QuarterlyEarnings,
    QuarterlyEarningsTimeline,
)
from app.stocks.earnings.quarterly.models import StockQuarterlyEarningsRecord
from app.stocks.earnings.quarterly.repository import (
    QuarterlyEarningsRepository,
    RefreshTarget,
)


def _quarter_key(quarter: QuarterlyEarnings) -> tuple[int, int]:
    return (quarter.fiscal_year, quarter.fiscal_quarter)


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
    )


def _to_timeline(
    symbol: str, rows: list[StockQuarterlyEarningsRecord]
) -> QuarterlyEarningsTimeline:
    """Rebuild the timeline in its canonical chronological order — ascending by
    ``(fiscal_year, fiscal_quarter)``, oldest reported quarter through furthest upcoming
    — the order the entity documents, regardless of the row order the query returned."""
    quarters = sorted((_to_entity(row) for row in rows), key=_quarter_key)
    return QuarterlyEarningsTimeline(symbol=symbol, quarters=tuple(quarters))


class SqlQuarterlyEarningsRepository(QuarterlyEarningsRepository):
    """Reads and writes the quarterly-earnings cache through a request-scoped session.

    Holds the session the router injects via ``get_db`` (the same shape as
    ``SqlAnalystEstimatesRepository``), maps rows to and from the ``QuarterlyEarnings``
    entities, and delegates every query to ``models``. ``upsert`` commits its own write
    so a successful cache fill is durable independent of the surrounding request.
    """

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
                    fetched_at=now,
                )
            )
        self._session.commit()

    def refresh_targets(self, limit: int) -> list[RefreshTarget]:
        # Delegates the query to models (stalest-first); this layer just wraps each
        # (symbol, name) pair in the domain-facing RefreshTarget.
        return [
            RefreshTarget(symbol, name)
            for symbol, name in models.stalest_symbols(self._session, limit)
        ]
