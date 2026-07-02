"""Interface Adapter: the SQLAlchemy-backed AnnualEarningsRepository.

Implements the ``repository.py`` port against the database. Its job is the mapping the use
cases must not see: it converts the ``AnnualEarnings`` entities to and from the ORM rows,
and delegates every query to ``models.py``. Only this layer (and models) knows the tables
exist; the domain entities stay free of SQLAlchemy. ``upsert`` rewrites a stock's whole
window (delete-then-insert) and commits its own write, so a successful cache fill is
durable independent of the request.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.stocks.earnings.annual import models
from app.stocks.earnings.annual.entities import (
    AnnualEarnings,
    AnnualEarningsTimeline,
)
from app.stocks.earnings.annual.models import StockAnnualEarningsRecord
from app.stocks.earnings.annual.repository import (
    AnnualEarningsRepository,
    RefreshTarget,
)


def _to_entity(row: StockAnnualEarningsRecord) -> AnnualEarnings:
    return AnnualEarnings(
        fiscal_year=row.fiscal_year,
        period_end=row.period_end,
        eps_actual=row.eps_actual,
        eps_estimate=row.eps_estimate,
        revenue_actual=row.revenue_actual,
        revenue_estimate=row.revenue_estimate,
        net_income=row.net_income,
        eps_actual_consensus=row.eps_actual_consensus,
    )


def _to_timeline(
    symbol: str, rows: list[StockAnnualEarningsRecord]
) -> AnnualEarningsTimeline:
    """Rebuild the timeline in its canonical chronological order — ascending by
    ``fiscal_year``, oldest reported year through furthest upcoming — the order the entity
    documents, regardless of the row order the query returned."""
    years = sorted((_to_entity(row) for row in rows), key=lambda y: y.fiscal_year)
    return AnnualEarningsTimeline(symbol=symbol, years=tuple(years))


class SqlAnnualEarningsRepository(AnnualEarningsRepository):
    """Reads and writes the annual-earnings cache through a request-scoped session.

    Holds the session the router injects via ``get_db``, maps rows to and from the
    ``AnnualEarnings`` entities, and delegates every query to ``models``. ``upsert`` commits
    its own write so a successful cache fill is durable independent of the surrounding
    request.
    """

    def __init__(self, session: Session, *, now=None) -> None:
        self._session = session
        # Injectable clock keeps the fetch stamp deterministic in tests.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def get(self, symbol: str) -> AnnualEarningsTimeline | None:
        rows = models.years_by_symbol(self._session, symbol)
        if not rows:
            return None
        return _to_timeline(symbol, rows)

    def upsert(
        self, symbol: str, name: str | None, timeline: AnnualEarningsTimeline
    ) -> None:
        stock = models.get_or_create_stock(self._session, symbol, name)

        # Rewrite the whole window: clear the stock's rows, then insert the new set.
        # Simpler and correct for a variable-length set of years than diffing.
        models.delete_years_for_stock(self._session, stock.id)
        now = self._now()
        for year in timeline.years:
            self._session.add(
                StockAnnualEarningsRecord(
                    stock_id=stock.id,
                    fiscal_year=year.fiscal_year,
                    period_end=year.period_end,
                    eps_actual=year.eps_actual,
                    eps_estimate=year.eps_estimate,
                    revenue_actual=year.revenue_actual,
                    revenue_estimate=year.revenue_estimate,
                    net_income=year.net_income,
                    eps_actual_consensus=year.eps_actual_consensus,
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
