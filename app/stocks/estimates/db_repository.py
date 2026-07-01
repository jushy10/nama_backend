"""Interface Adapter: the SQLAlchemy-backed AnalystEstimatesRepository.

Implements the ``repository.py`` port against the database. Its job is the mapping the
use case must not see: it converts the ``AnalystEstimates`` entity to and from the ORM
rows, and delegates every query to ``models.py``. Only this layer (and models) knows
the tables exist; the domain entity stays free of SQLAlchemy. ``upsert`` commits its
own write so a successful cache fill is durable independent of the request.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.stocks.entities import AnalystEstimates, ForwardEstimate
from app.stocks.estimates import models
from app.stocks.estimates.models import StockAnalystEstimatesRecord
from app.stocks.estimates.repository import (
    AnalystEstimatesRepository,
    CachedEstimates,
    RefreshTarget,
)


def _fy2_revenue(estimates: AnalystEstimates) -> float | None:
    """FY2's consensus revenue, pulled from the forward series (the headline fields
    carry FY2 EPS but not revenue). ``None`` when the series doesn't reach FY2."""
    years = estimates.forward_years
    return years[1].revenue_avg if len(years) > 1 else None


def _to_entity(row: StockAnalystEstimatesRecord) -> AnalystEstimates:
    """Rebuild the ``AnalystEstimates`` entity from a stored row.

    The two-row forward series is reconstructed from the FY1/FY2 columns so the
    entity's forward-growth methods work as if it came straight from the live
    provider. The series
    rows need a ``period_end``; FY1's is stored, FY2's is synthesized from its fiscal
    year (the value is never surfaced — only the EPS/revenue are read by the growth
    math).
    """
    forward_years: list[ForwardEstimate] = []
    if row.fiscal_year is not None:
        forward_years.append(
            ForwardEstimate(
                fiscal_year=row.fiscal_year,
                period_end=row.period_end or date(row.fiscal_year, 12, 31),
                eps_avg=row.eps_avg,
                revenue_avg=row.revenue_avg,
            )
        )
    if row.fiscal_year_fy2 is not None:
        forward_years.append(
            ForwardEstimate(
                fiscal_year=row.fiscal_year_fy2,
                period_end=date(row.fiscal_year_fy2, 12, 31),
                eps_avg=row.eps_avg_fy2,
                revenue_avg=row.revenue_avg_fy2,
            )
        )
    return AnalystEstimates(
        fiscal_year=row.fiscal_year,
        period_end=row.period_end,
        eps_avg=row.eps_avg,
        eps_low=row.eps_low,
        eps_high=row.eps_high,
        revenue_avg=row.revenue_avg,
        num_analysts_eps=row.num_analysts_eps,
        num_analysts_revenue=row.num_analysts_revenue,
        eps_avg_fy2=row.eps_avg_fy2,
        fiscal_year_fy2=row.fiscal_year_fy2,
        forward_years=tuple(forward_years),
    )


class SqlAnalystEstimatesRepository(AnalystEstimatesRepository):
    """Reads and writes the analyst-estimates cache through a request-scoped session.

    Holds the session the router injects via ``get_db`` (the same shape as
    ``SqlConstituentRepository``), maps rows to and from the ``AnalystEstimates``
    entity, and delegates every query to ``models``. ``upsert`` commits its own write
    so a successful cache fill is durable independent of the surrounding request.
    """

    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the fetch stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get(self, symbol: str) -> CachedEstimates | None:
        row = models.estimates_by_symbol(self._session, symbol)
        if row is None:
            return None
        return CachedEstimates(_to_entity(row), row.fetched_at)

    def upsert(
        self, symbol: str, name: str | None, estimates: AnalystEstimates
    ) -> None:
        stock = models.get_or_create_stock(self._session, symbol, name)

        row = models.estimates_by_stock_id(self._session, stock.id)
        if row is None:
            row = StockAnalystEstimatesRecord(stock_id=stock.id)
            self._session.add(row)

        row.fiscal_year = estimates.fiscal_year
        row.period_end = estimates.period_end
        row.eps_avg = estimates.eps_avg
        row.eps_low = estimates.eps_low
        row.eps_high = estimates.eps_high
        row.revenue_avg = estimates.revenue_avg
        row.num_analysts_eps = estimates.num_analysts_eps
        row.num_analysts_revenue = estimates.num_analysts_revenue
        row.fiscal_year_fy2 = estimates.fiscal_year_fy2
        row.eps_avg_fy2 = estimates.eps_avg_fy2
        row.revenue_avg_fy2 = _fy2_revenue(estimates)
        row.fetched_at = self._now()
        self._session.commit()

    def refresh_targets(self, limit: int) -> list[RefreshTarget]:
        # Delegates the query to models (oldest-fetched first); this layer just wraps
        # each (symbol, name) pair in the domain-facing RefreshTarget.
        return [
            RefreshTarget(symbol, name)
            for symbol, name in models.stalest_symbols(self._session, limit)
        ]
